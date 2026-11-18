"""Local CT import, geometry audit, orthogonal MPR, and physical distances."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import sys
import warnings
from zipfile import ZipFile, is_zipfile

with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    import numpy as np
    import pydicom
    from pydicom.uid import CTImageStorage
    from PIL import Image, ImageDraw
    from scipy.ndimage import map_coordinates

GEOMETRY_TOL_MM = 0.02
DIRECTION_TOL = 1e-4


@contextmanager
def source_items(source):
    if source.is_dir():
        files = sorted(p for p in source.rglob('*') if p.is_file() and not p.is_symlink())
    elif source.is_file():
        files = [source]
    else:
        raise ValueError('Input does not exist.')
    with ExitStack() as stack:
        items = []
        for file in files:
            if file.suffix.lower() == '.zip' or (len(files) == 1 and is_zipfile(file)):
                archive = stack.enter_context(ZipFile(file))
                items.extend((archive.open, entry, 'r') for entry in archive.infolist() if not entry.is_dir())
            else:
                items.append((Path.open, file, 'rb'))
        yield items, lambda item: item[0](item[1], item[2])


def vector(values, size):
    x = np.asarray(values, dtype=float)
    if x.shape != (size,) or not np.isfinite(x).all():
        raise ValueError('Invalid geometry vector.')
    return x


def scan(items, opener):
    groups = defaultdict(list)
    skipped = defaultdict(int)
    for item in items:
        try:
            with opener(item) as stream:
                d = pydicom.dcmread(stream, stop_before_pixels=True)
            if (d.get('Modality') != 'CT' or d.get('SOPClassUID') != CTImageStorage
                    or int(d.get('NumberOfFrames', 1)) != 1):
                skipped['not_classic_single_frame_ct'] += 1
                continue
            groups[str(d.SeriesInstanceUID)].append((item, d))
        except (KeyError, AttributeError, OSError, TypeError, ValueError, pydicom.errors.InvalidDicomError):
            skipped['unreadable_or_not_dicom'] += 1
    return [groups[k] for k in sorted(groups)], dict(skipped)


def geometry(records):
    first = records[0][1]
    u, v = vector(first.ImageOrientationPatient, 6).reshape(2, 3)
    if not np.allclose([u @ u, v @ v, u @ v], [1, 1, 0], atol=DIRECTION_TOL, rtol=0):
        raise ValueError('Non-orthonormal image orientation.')
    normal = np.cross(u, v)
    normal /= np.linalg.norm(normal)
    spacing = vector(first.PixelSpacing, 2)
    shape = (int(first.Rows), int(first.Columns))
    if np.any(spacing <= 0) or min(shape) <= 0:
        raise ValueError('Invalid pixel spacing or dimensions.')
    if len(records) < 2:
        raise ValueError('Fewer than two slices.')
    identities = set()
    positioned = []
    for item, d in records:
        image_type = {str(t).upper() for t in d.get('ImageType', [])}
        if 'LOCALIZER' in image_type or 'SCOUT' in image_type:
            raise ValueError('Localizer/scout series.')
        if d.SOPInstanceUID in identities:
            raise ValueError('Duplicated SOP identity.')
        identities.add(d.SOPInstanceUID)
        if (not first.get('FrameOfReferenceUID') or d.get('FrameOfReferenceUID') != first.FrameOfReferenceUID
                or d.get('StudyInstanceUID') != first.get('StudyInstanceUID')
                or (int(d.Rows), int(d.Columns)) != shape
                or not np.allclose(vector(d.PixelSpacing, 2), spacing, rtol=0, atol=1e-5)
                or not np.allclose(vector(d.ImageOrientationPatient, 6), np.r_[u, v], rtol=0, atol=DIRECTION_TOL)):
            raise ValueError('Inconsistent frame, dimensions, spacing, or orientation.')
        pos = vector(d.ImagePositionPatient, 3)
        positioned.append((float(pos @ normal), pos, item, d))
    positioned.sort(key=lambda r: r[0])
    distances = np.diff([r[0] for r in positioned])
    if np.min(distances) <= 0.001:
        raise ValueError('Duplicate or nearly duplicate slice positions.')
    if np.max(np.abs(distances - np.median(distances))) > GEOMETRY_TOL_MM:
        raise ValueError('Nonuniform slice spacing; check missing/repeated positions.')
    positions = np.asarray([r[1] for r in positioned])
    step = (positions[-1] - positions[0]) / (len(positions) - 1)
    residual = np.max(np.linalg.norm(positions - (positions[0] + np.arange(len(positions))[:, None] * step), axis=1))
    if residual > GEOMETRY_TOL_MM:
        raise ValueError('Slice positions do not fit one affine.')
    affine = np.eye(4)
    affine[:3, :3] = np.column_stack((step, v * spacing[0], u * spacing[1]))
    affine[:3, 3] = positions[0]
    thickness = sorted({float(d.SliceThickness) for _, d in records if d.get('SliceThickness')})
    result = {
        'shape_krc': [len(records), *shape], 'affine_krc_to_lps_mm': affine.tolist(),
        'pixel_spacing_row_col_mm': spacing.tolist(), 'slice_center_spacing_mm': float(step @ normal),
        'slice_thickness_values_mm': thickness, 'max_position_residual_mm': float(residual),
        'in_plane_step_mm': float(np.linalg.norm(step - (step @ normal) * normal)),
    }
    return result, [(item, d) for _, _, item, d in positioned]


def decode(records, opener, shape):
    volume = np.empty(shape, dtype=np.float32)
    all_hu = True
    order = []
    for k, (item, header) in enumerate(records):
        try:
            with opener(item) as stream:
                d = pydicom.dcmread(stream)
            pixels = d.pixel_array
            slope, offset = float(d.RescaleSlope), float(d.RescaleIntercept)
            if pixels.shape != tuple(shape[1:]) or slope == 0 or not np.isfinite([slope, offset]).all():
                raise ValueError
            if d.get('ModalityLUTSequence') or d.get('RealWorldValueMappingSequence'):
                raise ValueError
            scaled = pixels.astype(np.float32) * slope + offset
            if not np.isfinite(scaled).all():
                raise ValueError
        except Exception as exc:
            raise ValueError(f'Cannot decode/calibrate physical slice {k} ({type(exc).__name__}).') from None
        image_type = [str(t).upper() for t in d.get('ImageType', [])]
        expected_hu = bool(len(image_type) >= 3 and image_type[0] == 'ORIGINAL'
                           and image_type[2] not in {'LOCALIZER', 'SCOUT'}
                           and str(d.get('MultiEnergyCTAcquisition', 'NO')).upper() == 'NO')
        all_hu &= str(d.get('RescaleType', 'HU' if expected_hu else '')).upper() == 'HU'
        volume[k] = scaled
        order.append(int(header.InstanceNumber) if header.get('InstanceNumber') is not None else None)
    return volume, all_hu, order


def inside(point, shape):
    return np.isfinite(point).all() and (point >= 0).all() and (point <= np.asarray(shape) - 1).all()


def mpr(volume, affine, center, ww, wl, destination):
    basis = affine[:3, :3]
    origin = affine[:3, 3]
    inverse = np.linalg.inv(basis)
    center_lps = origin + basis @ center
    bounds = np.array(np.meshgrid(*[(0, n-1) for n in volume.shape])).reshape(3, -1).T @ basis.T + origin
    lo, hi = bounds.min(axis=0), bounds.max(axis=0)
    requested_pitch = min(np.linalg.norm(basis[:, 1]), np.linalg.norm(basis[:, 2]), abs(np.linalg.det(basis)) / np.linalg.norm(np.cross(basis[:, 1], basis[:, 2])))
    pitch = max(requested_pitch, float(max(hi - lo)) / 900)
    axes = [np.linspace(a, b, max(2, int(np.ceil((b-a)/pitch))+1)) for a, b in zip(lo, hi)]
    views = [('axial', 0, 1, 2, 'R to L / A to P', False),
             ('coronal', 0, 2, 1, 'R to L / S to I', True),
             ('sagittal', 1, 2, 0, 'A to P / S to I', True)]
    manifest = {}
    for name, col, row, fixed, label, reverse in views:
        horiz, vert = axes[col], axes[row][::-1] if reverse else axes[row]
        x, y = np.meshgrid(horiz, vert)
        lps = np.empty((3, *x.shape))
        lps[col], lps[row], lps[fixed] = x, y, center_lps[fixed]
        krc = np.einsum('ij,jhw->ihw', inverse, lps-origin[:, None, None])
        # Eliminate floating-point spill at the exact voxel boundary.
        krc = krc.round(9)
        pixels = map_coordinates(volume, krc, order=1, mode='constant', cval=wl-ww, prefilter=False)
        gray = (np.clip((pixels - (wl-ww/2))/ww, 0, 1)*255).astype(np.uint8)
        raw = Image.fromarray(gray)
        canvas = Image.new('L', (max(350, raw.width), raw.height + 34))
        canvas.paste(raw, (0, 34))
        ImageDraw.Draw(canvas).text((7, 7), f'{name} | {label} | WW {ww:g} WL {wl:g}', fill=255)
        canvas.save(destination / f'{name}.png')
        manifest[name] = {'top_left_pixel_lps_mm': lps[:, 0, 0].tolist(),
                          'column_step_lps_mm': (lps[:, 0, 1]-lps[:, 0, 0]).tolist(),
                          'row_step_lps_mm': (lps[:, 1, 0]-lps[:, 0, 0]).tolist(),
                          'image_origin_pixel_xy': [0, 34], 'image_rows_cols': list(gray.shape)}
    return manifest


def run(args):
    if args.command == 'distance':
        info = json.loads(args.geometry.read_text(encoding='utf-8'))
        a, b = np.asarray(args.a), np.asarray(args.b)
        if not all(inside(p, info['shape_krc']) for p in (a, b)):
            raise ValueError('Endpoint outside the volume.')
        transform = np.asarray(info['affine_krc_to_lps_mm'])
        a_lps, b_lps = (transform[:3, :3] @ p + transform[:3, 3] for p in (a, b))
        return {'endpoints_krc': [a.tolist(), b.tolist()], 'endpoints_lps_mm': [a_lps.tolist(), b_lps.tolist()],
                'endpoint_distance_mm': float(np.linalg.norm(a_lps-b_lps))}
    with source_items(args.source) as (items, opener):
        groups, skipped = scan(items, opener)
        if args.command == 'inventory':
            return {'source_entries': len(items), 'skipped': skipped, 'series': [
                {'group': i, 'series_number': int(g[0][1].get('SeriesNumber', 0)), 'count': len(g),
                 'rows_cols': [int(g[0][1].get('Rows', 0)), int(g[0][1].get('Columns', 0))]}
                for i, g in enumerate(groups, 1)]}
        if not 1 <= args.group <= len(groups):
            raise ValueError('Select an inventory group using --group.')
        info, ordered = geometry(groups[args.group-1])
        if args.expected_count is not None and args.expected_count != len(ordered):
            raise ValueError('Independent viewer count differs from the selected series.')
        shape = info['shape_krc']
        center = np.asarray(args.center if args.center is not None else (np.asarray(shape)-1)/2)
        if not inside(center, shape):
            raise ValueError('MPR center outside the volume.')
        volume, hu, order = decode(ordered, opener, shape)
    info.update(group=args.group, series_number=int(ordered[0][1].get('SeriesNumber', 0)),
                expected_viewer_count=args.expected_count, count_matches_viewer=True if args.expected_count is not None else None,
                decoded_slices=len(ordered), units='HU' if hu else 'rescaled unit unverified',
                instance_numbers_in_physical_order=order, center_krc=center.tolist(), ww=args.ww, wl=args.wl)
    args.out.mkdir(parents=True, exist_ok=False)
    info['mpr'] = mpr(volume, np.asarray(info['affine_krc_to_lps_mm']), center, args.ww, args.wl, args.out)
    np.save(args.out / 'volume.npy', volume, allow_pickle=False)
    (args.out / 'geometry.json').write_text(json.dumps(info, indent=2, allow_nan=False), encoding='utf-8')
    return {k: info[k] for k in ('shape_krc', 'pixel_spacing_row_col_mm', 'slice_center_spacing_mm',
                                 'slice_thickness_values_mm', 'max_position_residual_mm', 'in_plane_step_mm',
                                 'count_matches_viewer', 'decoded_slices', 'units')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('inventory').add_argument('source', type=Path)
    p = sub.add_parser('prepare')
    p.add_argument('source', type=Path)
    p.add_argument('--group', type=int, required=True)
    p.add_argument('--expected-count', type=int)
    p.add_argument('--center', type=float, nargs=3, metavar=('K', 'R', 'C'))
    p.add_argument('--ww', type=float, default=1800)
    p.add_argument('--wl', type=float, default=450)
    p.add_argument('--out', type=Path, required=True)
    d = sub.add_parser('distance')
    d.add_argument('geometry', type=Path)
    d.add_argument('--a', type=float, nargs=3, required=True)
    d.add_argument('--b', type=float, nargs=3, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare' and (not np.isfinite([args.ww, args.wl]).all() or args.ww <= 0):
            raise ValueError('Invalid window width/level.')
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')  # Decoders may include private tag values in warnings.
            result = run(args)
        print(json.dumps(result, indent=2, allow_nan=False))
    except ValueError as exc:
        # Only deliberate validation messages should be rendered. DICOM exceptions can contain tag values.
        allowed = ('Input', 'Invalid ', 'Non-', 'Fewer', 'Localizer', 'Duplicat', 'Inconsistent', 'Nonuniform',
                   'Slice positions', 'Cannot decode', 'Endpoint', 'Select an inventory', 'Independent viewer', 'MPR center')
        msg = str(exc) if type(exc) is ValueError and str(exc).startswith(allowed) else 'Invalid input or geometry metadata.'
        print(msg, file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'Processing failed ({type(exc).__name__}). Check source, dependencies and output directory privately.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
