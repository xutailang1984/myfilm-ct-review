"""Exercise the installed skill with generated CT and a fully intercepted Chrome page."""

from contextlib import redirect_stdout
from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit
from zipfile import ZipFile

import numpy as np
from PIL import Image
from playwright.async_api import Browser
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid


SKILL = Path(__file__).resolve().parents[1] / "skills" / "myfilm-ct-review"
FIXTURE_CODE = "SyntheticGate"
FIXTURE_URL = "https://www.myfilm.cc/#/v/synthetic-ci-fixture"
STUDY_UID = generate_uid()
K, R, C = np.indices((4, 6, 8))


@dataclass
class Series:
    number: int
    side: str
    kernel: str
    uid: str = field(default_factory=generate_uid)

    def archive(self):
        frame_uid = generate_uid()
        pixels = 50 * K + 11 * R + 7 * C + self.number
        archive = io.BytesIO()
        with ZipFile(archive, "w") as grouped:
            for k in reversed(range(len(pixels))):
                meta = FileMetaDataset()
                meta.MediaStorageSOPClassUID = CTImageStorage
                meta.MediaStorageSOPInstanceUID = generate_uid()
                meta.TransferSyntaxUID = ExplicitVRLittleEndian
                ds = FileDataset(None, {}, file_meta=meta, preamble=bytes(128))
                ds.SOPClassUID = meta.MediaStorageSOPClassUID
                ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
                ds.StudyInstanceUID = STUDY_UID
                ds.SeriesInstanceUID = self.uid
                ds.FrameOfReferenceUID = frame_uid
                ds.SeriesNumber = self.number
                ds.InstanceNumber = k + 1
                ds.Modality = "CT"
                ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
                ds.BodyPartExamined = "KNEE" if self.side else "LSPINE"
                ds.ProtocolName = f"{self.side} KNEE" if self.side else "L SPINE"
                if self.side:
                    ds.Laterality = self.side
                ds.ConvolutionKernel = self.kernel
                ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
                ds.ImagePositionPatient = [10, -5, 18 + 1.5 * k]
                ds.PixelSpacing = [0.5, 0.75]
                ds.SliceThickness = 1.25
                ds.Rows, ds.Columns = pixels.shape[1:]
                ds.SamplesPerPixel = 1
                ds.PhotometricInterpretation = "MONOCHROME2"
                ds.BitsAllocated = ds.BitsStored = 16
                ds.HighBit = 15
                ds.PixelRepresentation = 1
                ds.RescaleSlope = 2
                ds.RescaleIntercept = -1000
                ds.RescaleType = "HU"
                ds.PixelData = pixels[k].astype("<i2").tobytes()
                encoded = io.BytesIO()
                ds.save_as(encoded, enforce_file_format=True)
                grouped.writestr(f"{k:03}.dcm", encoded.getvalue())
        return archive.getvalue()

    def viewer_tags(self):
        return {
            "0020,0011": str(self.number),
            "0020,000e": self.uid,
            "0008,0060": "CT",
            "0008,0016": str(CTImageStorage),
            "0008,0008": "ORIGINAL\\PRIMARY\\AXIAL",
            "0018,0015": "KNEE" if self.side else "LSPINE",
            "0018,1030": f"{self.side} KNEE" if self.side else "L SPINE",
            "0018,1210": self.kernel,
            "0020,0037": "1,0,0,0,1,0",
            "0020,0060": self.side,
        }


def share_page():
    return """<!doctype html><meta charset="utf-8">
      <form id="gate">
        <p>请输入分享链接的验证码</p>
        <input id="verification" type="password">
        <button type="submit">确 定</button>
      </form>
      <button id="view" hidden>查看影像</button>
      <script>
      const gate = document.querySelector('#gate');
      const view = document.querySelector('#view');
      gate.addEventListener('submit', event => {
        event.preventDefault();
        if (document.querySelector('#verification').value === fixtureCode) {
          gate.remove();
          view.hidden = false;
        }
      });
      view.addEventListener('click', () => {
        const frame = document.createElement('iframe');
        frame.className = 'image-frame';
        frame.width = 840;
        frame.height = 720;
        frame.src = '/__synthetic__/viewer';
        view.replaceWith(frame);
      });
      const fixtureCode = """ + json.dumps(FIXTURE_CODE) + ";</script>"


def viewer_page(series):
    return """<!doctype html><meta charset="utf-8">
      <style>
        .series-list-item { display: inline-block; margin: 4px; padding: 8px; }
        .image-view-container { width: 480px; height: 360px; background: #eee; }
      </style>
      <section id="series"></section>
      <button class="button-name"><span>DICOM信息</span></button>
      <div class="image-view-container"><canvas class="cornerstone-canvas" width="480" height="360"></canvas></div>
      <button id="export" hidden>导出此序列dicom图像</button>
      <dialog aria-label="DICOM信息">
        <table><tbody></tbody></table>
        <button id="close">关闭</button>
      </dialog>
      <script>
      const items = """ + json.dumps([s.viewer_tags() for s in series]) + """;
      let current = items[0];
      for (const tags of items) {
        const card = document.createElement('button');
        card.className = 'series-list-item';
        const img = document.createElement('img');
        img.className = 'series-thumb';
        img.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
        const count = document.createElement('div');
        count.textContent = '4';
        card.append(img, count);
        card.addEventListener('dblclick', () => { current = tags; });
        document.querySelector('#series').append(card);
      }
      const dialog = document.querySelector('dialog');
      document.querySelector('.button-name').addEventListener('click', () => {
        const table = dialog.querySelector('tbody');
        table.replaceChildren();
        for (const [key, value] of Object.entries(current)) {
          const row = table.insertRow();
          row.insertCell().textContent = key;
          row.insertCell().textContent = 'fixture';
          row.insertCell().textContent = value;
        }
        dialog.showModal();
      });
      document.querySelector('#close').addEventListener('click', () => dialog.close());
      const exportButton = document.querySelector('#export');
      document.querySelector('.image-view-container').addEventListener('contextmenu', event => {
        event.preventDefault();
        exportButton.hidden = false;
      });
      exportButton.addEventListener('click', async () => {
        const response = await fetch('/__synthetic__/archive/' + current['0020,0011']);
        if (!response.ok) return;
        const link = document.createElement('a');
        link.href = URL.createObjectURL(await response.blob());
        link.download = 'synthetic.zip';
        document.body.append(link);
        link.click();
        link.remove();
      });
      </script>"""


def fetch_test(scripts, output, series, archives, target, detail=None):
    sys.path.insert(0, str(scripts))
    import myfilm_fetch

    downloaded = []
    unexpected = []
    new_context = Browser.new_context
    archive_prefix = "/__synthetic__/archive/"

    async def route_fixture(route):
        url = urlsplit(route.request.url)
        if url.scheme == "https" and url.netloc == "www.myfilm.cc" and not url.query:
            if url.path == "/":
                await route.fulfill(content_type="text/html; charset=utf-8", body=share_page())
                return
            if url.path == "/__synthetic__/viewer":
                await route.fulfill(content_type="text/html; charset=utf-8", body=viewer_page(series))
                return
            if url.path.startswith(archive_prefix):
                number = url.path.removeprefix(archive_prefix)
                if number in archives:
                    downloaded.append(number)
                    await route.fulfill(content_type="application/zip", body=archives[number])
                    return
        unexpected.append(True)
        await route.abort("blockedbyclient")

    async def fixture_context(browser, **kwargs):
        context = await new_context(browser, service_workers="block", **kwargs)
        await context.route("**/*", route_fixture)
        return context

    args = ["myfilm_fetch.py", "fetch", "--input-stdin", "--browser", "chrome",
            "--target", target, "--out", str(output), "--export-timeout", "30"]
    if detail:
        args.extend(["--detail", detail])
    source = io.StringIO(json.dumps({"url": FIXTURE_URL, "code": FIXTURE_CODE}) + "\n")
    captured = io.StringIO()
    try:
        with (patch.object(Browser, "new_context", fixture_context),
              patch.object(sys, "argv", args), patch.object(sys, "stdin", source),
              redirect_stdout(captured)):
            exit_code = myfilm_fetch.main()
    finally:
        sys.path.pop(0)
    assert not unexpected, "Chrome tried a request outside the mock routes"
    assert FIXTURE_URL not in captured.getvalue() and FIXTURE_CODE not in captured.getvalue()
    events = [json.loads(line) for line in captured.getvalue().splitlines()]
    assert any(event.get("share_code_submitted") for event in events), events
    return exit_code, events, downloaded


def offline_test(scripts, source, output, series_number):
    def cli(*args):
        command = [sys.executable, "-B", str(scripts / "ct_review.py"), *map(str, args)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=45)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    inventory = cli("inventory", source)
    assert inventory == {"source_entries": 4, "skipped": {}, "series": [
        {"group": 1, "series_number": series_number, "count": 4, "rows_cols": [6, 8]}]}, inventory
    prepared = cli("prepare", source, "--group", 1, "--expected-count", 4,
                   "--ww", 1000, "--wl", -750, "--out", output)
    assert prepared["units"] == "HU" and prepared["count_matches_viewer"]
    expected_volume = 2 * (50 * K + 11 * R + 7 * C + series_number) - 1000
    np.testing.assert_array_equal(np.load(output / "volume.npy", allow_pickle=False), expected_volume)
    geometry_path = output / "geometry.json"
    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    assert geometry["instance_numbers_in_physical_order"] == [1, 2, 3, 4]
    np.testing.assert_allclose(geometry["affine_krc_to_lps_mm"],
                               [[0, 0, .75, 10], [0, .5, 0, -5], [1.5, 0, 0, 18], [0, 0, 0, 1]])
    for name, (top_left, bottom_right, shape) in {
        "axial": ((1.5, 0, 0), (1.5, 5, 7), (6, 12)),
        "coronal": ((3, 2.5, 0), (0, 2.5, 7), (10, 12)),
        "sagittal": ((3, 0, 3.5), (0, 5, 3.5), (10, 6)),
    }.items():
        view = geometry["mpr"][name]
        rows, cols = shape
        assert view["image_rows_cols"] == [rows, cols]
        x, y = view["image_origin_pixel_xy"]
        with Image.open(output / f"{name}.png") as image:
            samples = np.asarray(image)[y:y + rows, x:x + cols].astype(int)
        assert samples.shape == shape and np.ptp(samples) > 0
        for point, pixel in [(top_left, samples[0, 0]), (bottom_right, samples[-1, -1])]:
            k, r, c = point
            hu = 2 * (50 * k + 11 * r + 7 * c + series_number) - 1000
            assert abs(pixel - int((hu + 1250) * 255 / 1000)) <= 1, name
    distance = cli("distance", geometry_path, "--a", 0, 0, 0, "--b", 2, 2, 2)
    np.testing.assert_allclose(distance["endpoints_lps_mm"], [[10, -5, 18], [11.5, -4, 21]])
    assert distance["endpoint_distance_mm"] == 3.5
    print("PASS offline DICOM inventory, HU volume, axial/coronal/sagittal MPR, and 3.5 mm reference distance")


def main():
    with TemporaryDirectory(prefix="myfilm-ci-") as temporary:
        workspace = Path(temporary)
        skill = workspace / ".agents" / "skills" / SKILL.name
        shutil.copytree(SKILL, skill, ignore=shutil.ignore_patterns("__pycache__"))
        scripts = skill / "scripts"
        assert (skill / "SKILL.md").is_file() and (scripts / "requirements-web.txt").is_file()
        print("PASS clean project skill installation and dependency imports")
        series = [Series(41, "L", "B_VSHARP_C"), Series(42, "L", "B_SOFT_C"),
                  Series(51, "R", "B_VSHARP_C"), Series(52, "R", "B_SOFT_C"),
                  Series(61, "", "B_SOFT_C")]
        archives = {str(item.number): item.archive() for item in series}
        source = workspace / "left.zip"
        source.write_bytes(archives["41"])
        offline_test(scripts, source, workspace / "mpr", 41)

        output = workspace / "chrome.zip"
        code, events, downloads = fetch_test(scripts, output, series, archives, "左膝")
        assert code == 2 and events[-1]["reason"] == "multiple_anatomic_matches_need_reconstruction_intent", events
        assert not downloads and not output.exists()
        print("PASS Chrome submits the mock gate and stops on equally sized ambiguous left-knee candidates")

        code, events, downloads = fetch_test(scripts, output, series, archives, "左膝", "sharp")
        assert code == 0 and events[-1]["stage"] == "validated_dicom_zip", events
        assert events[-1]["decoded_slices"] == 4 and events[-1]["identity_matches_viewer"]
        assert downloads == ["41"] and output.read_bytes() == archives["41"]
        print("PASS Chrome chooses left-knee sharp series without a supplied series number and saves the exact ZIP")

        output = workspace / "lumbar.zip"
        code, events, downloads = fetch_test(scripts, output, series, archives, "腰椎")
        assert code == 0 and events[-1]["stage"] == "validated_dicom_zip", events
        assert downloads == ["61"] and output.read_bytes() == archives["61"]
        print("PASS Chrome chooses lumbar series by body part alone and validates the downloaded ZIP")

        output = workspace / "incorrect.zip"
        code, events, downloads = fetch_test(scripts, output, series,
                                             {**archives, "41": archives["51"]}, "左膝", "sharp")
        assert code == 2 and events[-1]["reason"] == "zip_series_identity_differs_from_viewer", events
        assert downloads == ["41"] and not output.exists()
        print("PASS Chrome export with the wrong series identity is rejected before publishing")


if __name__ == "__main__":
    main()
