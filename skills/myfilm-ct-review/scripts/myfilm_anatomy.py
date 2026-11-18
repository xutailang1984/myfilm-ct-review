"""Conservative anatomic selection for MyFilm's single-frame CT series."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import unicodedata


TAGS = ('0018,1030', '0008,103e', '0018,1210', '0020,0037')
ANATOMY_TAGS = (*TAGS, '0018,0015', '0020,0060', '0020,0062', '0008,0008')


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).upper().split())


@dataclass(frozen=True)
class Target:
    region: str
    side: str | None = None


def target_from_text(text):
    x = norm(text)
    if x in {'腰椎', '腰段脊柱', 'L SPINE', 'LUMBAR SPINE', 'LUMBAR', 'LSPINE'}:
        return Target('lumbar_spine')
    if x in {'左膝', '左膝关节', '左侧膝关节', '左侧膝', 'LEFT KNEE', 'L KNEE'}:
        return Target('knee', 'L')
    if x in {'右膝', '右膝关节', '右侧膝关节', '右侧膝', 'RIGHT KNEE', 'R KNEE'}:
        return Target('knee', 'R')
    return None


REGIONS = {
    'knee': r'膝|(?<![A-Z])KNEE(?![A-Z])',
    'lumbar_spine': r'腰椎|腰段|(?<![A-Z])(?:L[ _-]*SPINE|LUMBAR)(?![A-Z])',
    'cervical_spine': r'颈椎|(?<![A-Z])(?:C[ _-]*SPINE|CERVICAL)(?![A-Z])',
    'thoracic_spine': r'胸椎|(?<![A-Z])(?:T[ _-]*SPINE|THORACIC SPINE)(?![A-Z])',
    'chest': r'胸部|肺|(?<![A-Z])(?:CHEST|LUNG)(?![A-Z])',
    'head': r'头颅|(?<![A-Z])(?:HEAD|BRAIN)(?![A-Z])',
    'abdomen': r'腹部|(?<![A-Z])ABDOMEN(?![A-Z])',
    'pelvis': r'骨盆|盆腔|(?<![A-Z])PELVIS(?![A-Z])',
    'hip': r'髋|(?<![A-Z])HIP(?![A-Z])',
    'ankle': r'踝|(?<![A-Z])ANKLE(?![A-Z])',
    'foot': r'足部|(?<![A-Z])FOOT(?![A-Z])',
}

SIDE_RE = {
    'L': r'左(?:侧)?[ _-]*膝|膝(?:关节)?[ _:()/-]*左|(?<![A-Z0-9])(?:L|LT|LEFT)[ _-]+KNEE(?![A-Z0-9])|(?<![A-Z0-9])KNEE[ _-]+(?:L|LT|LEFT)(?![A-Z0-9])',
    'R': r'右(?:侧)?[ _-]*膝|膝(?:关节)?[ _:()/-]*右|(?<![A-Z0-9])(?:R|RT|RIGHT)[ _-]+KNEE(?![A-Z0-9])|(?<![A-Z0-9])KNEE[ _-]+(?:R|RT|RIGHT)(?![A-Z0-9])',
    'B': r'双(?:侧)?[ _-]*膝|(?<![A-Z])BILATERAL[ _-]+KNEES?(?![A-Z])',
}


def kernel_detail(value):
    keywords = set(re.findall(r'[A-Z0-9]+', norm(value)))
    sharp = bool(keywords & {'SHARP', 'VSHARP', 'BONE'})
    soft = bool(keywords & {'SOFT', 'SMOOTH'})
    return 'conflict' if sharp and soft else 'sharp' if sharp else 'soft' if soft else 'unknown'


def numbers(value, size):
    # The viewer displays multi-values as separated numbers; don't extract digits from arbitrary text.
    parts = re.split(r'[\\,\s;]+', norm(value).strip('[]()'))
    if len(parts) != size or not all(re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?', p) for p in parts):
        return None
    result = tuple(float(p) for p in parts)
    return result if all(math.isfinite(x) for x in result) else None


def transverse(meta):
    iop = numbers(meta.get('0020,0037'), 6)
    if iop is None:
        return None
    a, b, c, d, e, f = iop
    if abs(a*a+b*b+c*c-1) > .002 or abs(d*d+e*e+f*f-1) > .002 or abs(a*d+b*e+c*f) > .002:
        return None
    return abs(a*e-b*d) >= .95


def classify(meta):
    body_sources = {'body_part': norm(meta.get('0018,0015')), 'protocol': norm(meta.get('0018,1030')),
                    'series_description': norm(meta.get('0008,103e'))}
    region_hits = {k: {r for r, pat in REGIONS.items() if re.search(pat, v)} for k, v in body_sources.items()}
    regions = set().union(*region_hits.values())
    side_hits = {k: {s for s, pat in SIDE_RE.items() if re.search(pat, v)} for k, v in body_sources.items()}
    for key, tag in (('image_laterality', '0020,0062'), ('series_laterality', '0020,0060')):
        v = norm(meta.get(tag))
        if v in {'L', 'R', 'B', 'U'}:
            side_hits[key] = {v}
    sides = set().union(*side_hits.values())
    # A series description alone is useful for finding a candidate, never sufficient for auto-selection.
    return {'regions': sorted(regions), 'sides': sorted(sides),
            'confirmed_regions': sorted(region_hits['body_part'] | region_hits['protocol']),
            'confirmed_sides': sorted(set().union(*(v for k, v in side_hits.items() if k != 'series_description'))),
            'region_evidence': sorted(k for k, v in region_hits.items() if v),
            'side_evidence': sorted(k for k, v in side_hits.items() if v),
            'kernel_detail': kernel_detail(meta.get('0018,1210')), 'transverse': transverse(meta)}


def relevance(info, target):
    """Return match/exclude/uncertain. Missing metadata on a plausible source prevents choosing."""
    regions, sides = info['regions'], info['sides']
    if regions and target.region not in regions:
        return 'exclude' if len(regions) == 1 and regions == info['confirmed_regions'] else 'uncertain'
    if target.side and sides == ['R' if target.side == 'L' else 'L'] and sides == info['confirmed_sides']:
        return 'exclude'
    if regions != [target.region] or target.region not in info['confirmed_regions']:
        return 'uncertain'
    if not target.side and sides not in ([], ['U']):
        return 'uncertain'  # A lumbar target should not be narrowed to a unilateral series by an unexplained label.
    if target.side and (sides != [target.side] or target.side not in info['confirmed_sides']):
        return 'uncertain'
    return 'match'


def header_metadata(ds):
    fields = {}
    for key in ANATOMY_TAGS:
        item = ds.get(tuple(int(n, 16) for n in key.split(',')))
        if item is not None and item.value is not None:
            fields[key] = ('\\'.join(str(x) for x in item.value) if item.VM > 1 else str(item.value))
    return fields
