"""Use MyFilm's normal share page to inventory or export an authorized CT series."""
from __future__ import annotations

import argparse
import asyncio
from getpass import GetPassWarning, getpass
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
import warnings
from zipfile import ZipFile

import myfilm_anatomy as anatomy


def note(**result):
    print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)


class FetchError(Exception):
    """Safe stage/detail tokens for stderr or JSON. Never include a page response."""


def share_input(piped):
    if piped:
        data = json.loads(sys.stdin.readline(6000))
        url, code = data['url'], data.get('code', '')
    else:
        if not sys.stdin.isatty():
            raise FetchError('secret_input_requires_terminal_or_private_stdin')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', GetPassWarning)
                url, code = getpass('MyFilm share URL: '), getpass('MyFilm share code: ')
        except GetPassWarning:
            raise FetchError('terminal_cannot_protect_secret_input') from None
    if not isinstance(url, str) or not isinstance(code, str):
        raise FetchError('invalid_input')
    url, code = url.strip(), code.strip()
    u = urlsplit(url)
    if (u.scheme != 'https' or u.netloc not in {'www.myfilm.cc', 'myfilm.cc'}
            or u.query or u.path not in {'', '/'} or not re.fullmatch(r'/v/[A-Za-z0-9_-]{3,100}', u.fragment)):
        raise FetchError('unsupported_share_link')
    if code and not re.fullmatch(r'[A-Za-z0-9]{1,16}', code):
        raise FetchError('unsupported_share_code_format')
    return url, code


def trusted_site(url):
    u = urlsplit(url)
    return (u.scheme == 'https' and not u.username and not u.password and u.port in (None, 443)
            and bool(u.hostname) and (u.hostname == 'myfilm.cc' or u.hostname.endswith('.myfilm.cc')))


def trusted_share_origin(page_url):
    u = urlsplit(page_url)
    return trusted_site(page_url) and u.hostname in {'myfilm.cc', 'www.myfilm.cc'}


def check_frame(frame):
    if frame.is_detached() or not trusted_site(frame.url):
        raise FetchError('unexpected_viewer_origin')


async def wait_for_state(locators, seconds=25):
    until = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < until:
        for key, locator in locators:
            if await locator.count() == 1 and await locator.is_visible():
                return key
        await asyncio.sleep(0.2)
    raise FetchError('expected_page_controls_not_ready')


async def enter_viewer(page, url, code):
    await page.goto(url, wait_until='domcontentloaded', timeout=35000)
    if not trusted_share_origin(page.url):
        raise FetchError('unexpected_signin_origin')
    note(stage='share_page_reached')
    viewer = page.get_by_role('button', name='查看影像', exact=True)
    submit = page.get_by_role('button', name=re.compile(r'^确\s*定$')).filter(visible=True)
    gate = page.get_by_text(re.compile('请输入分享链接的验证码'))
    state = await wait_for_state([('viewer', viewer), ('gate', gate)], seconds=35)
    if state == 'gate':
        field = page.locator('input:not([type]), input[type=text], input[type=password], input[type=number], input[type=tel]').filter(visible=True)
        try:
            await field.first.wait_for(state='visible', timeout=7000)
            await submit.first.wait_for(state='visible', timeout=7000)
        except Exception:
            pass  # Emit only counts below if the known controls did not appear.
        # The two official share-page variants can change their in-page hash while showing the gate.
        control_state = {'origin_matches_requested_share': trusted_share_origin(page.url),
                         'visible_code_inputs': await field.count(), 'visible_confirm_buttons': await submit.count()}
        if not code or control_state != {'origin_matches_requested_share': True, 'visible_code_inputs': 1, 'visible_confirm_buttons': 1}:
            note(stage='share_verification_controls_unmatched', **control_state)
            raise FetchError('share_code_or_supported_prompt_missing')
        await field.fill(code)
        await submit.click()
        note(stage='normal_share_form_submitted')
        try:
            await viewer.wait_for(state='visible', timeout=30000)
        except Exception:
            # Only report whether a known status is present in the visible page.
            flags = {}
            for name, message in (
                ('code_rejected', r'验证码.{0,6}(?:错误|不正确|无效)|(?:错误|无效).{0,6}验证码'),
                ('share_expired', r'(?:链接|分享|报告).{0,8}(?:到期|过期|失效)|已过期|已失效'),
                ('challenge_notice', r'人机验证|安全验证|访问验证'),
                ('retry_later', r'请稍后重试|操作频繁|请求失败'),
            ):
                flags[name] = bool(await page.get_by_text(re.compile(message)).filter(visible=True).count())
            note(stage='share_result_not_recognized', **flags,
                 still_on_verification=(await field.count() == 1 and await gate.count() >= 1),
                 viewer_frame_visible=bool(await page.locator('iframe.image-frame').filter(visible=True).count()),
                 related_pages=len(page.context.pages))
            raise FetchError('viewer_not_reached_after_share_form') from None
    if not trusted_share_origin(page.url):
        raise FetchError('unexpected_share_route_after_verification')
    await viewer.click()
    note(stage='view_images_clicked')
    iframe = page.locator('iframe.image-frame').filter(visible=True)
    try:
        await iframe.wait_for(state='visible', timeout=35000)
    except Exception:
        note(stage='viewer_frame_unavailable', iframe_elements=await page.locator('iframe').count(),
             related_pages=len(page.context.pages), expected_top_origin=trusted_share_origin(page.url))
        raise FetchError('supported_viewer_frame_not_visible') from None
    note(stage='viewer_frame_visible')
    frame_handle = await iframe.element_handle()
    child = await frame_handle.content_frame() if frame_handle is not None else None
    if child is None:
        raise FetchError('unsupported_viewer_frame')
    # about:blank may be an initialization state while the visible viewer loads.
    if child.url not in ('', 'about:blank') and not trusted_site(child.url):
        raise FetchError('unexpected_viewer_origin')
    cards = child.locator('.series-list-item:has(img.series-thumb)').filter(visible=True)
    try:
        await cards.first.wait_for(state='visible', timeout=55000)
    except Exception:
        note(stage='viewer_series_unavailable', items=await child.locator('.series-list-item').count(),
             thumbnails=await child.locator('img.series-thumb').count(),
             origin_ok=trusted_site(child.url), attached=not child.is_detached())
        raise FetchError('viewer_loaded_without_supported_series_catalog') from None
    check_frame(child)
    note(stage='authorized_viewer_open', share_code_submitted=state == 'gate')
    return child, cards


async def catalog(cards):
    # Count candidates using visible, short, purely numeric fields; never print card descriptions.
    result = await cards.evaluate_all(r'''nodes => nodes.map((node, i) => {
      const lines = node.innerText.split(/\n+/).map(x => x.trim());
      return {item: i + 1, numeric_labels: lines.filter(x => /^\d{1,4}$/.test(x)).map(Number)};
    })''')
    if not result or len(result) > 100:
        raise FetchError('unexpected_catalog_size')
    return result


async def current_metadata(frame):
    check_frame(frame)
    button = frame.locator('.button-name').get_by_text('DICOM信息', exact=True)
    await button.click()
    dialog = frame.get_by_role('dialog', name='DICOM信息')
    try:
        await dialog.wait_for(state='visible', timeout=5000)
        rows = dialog.locator('tr')
        await rows.first.wait_for(state='visible', timeout=5000)
        tags = ['0020,0011', '0020,000e', '0008,0060', '0008,0016', *anatomy.ANATOMY_TAGS]
        return await rows.evaluate_all(r'''(rows, wanted) => {
          const out = {};
          for (const row of rows) {
            const children = row.children;
            const tag = children[0]?.innerText?.trim().toLowerCase();
            if (children.length >= 3 && wanted.includes(tag))
              out[tag] = children[children.length - 1].innerText.trim();
          }
          return out;
        }''', tags)
    finally:
        close = dialog.get_by_role('button', name='关闭', exact=True)
        if await close.count() == 1 and await close.is_visible():
            await close.click(timeout=4000)


async def activate(frame, cards, item, previous_uid=None):
    check_frame(frame)
    await cards.nth(item - 1).dblclick()
    await frame.locator('canvas.cornerstone-canvas').filter(visible=True).first.wait_for(state='visible', timeout=15000)
    # The canvas can still show the preceding series briefly after double-click.
    for _ in range(5):
        meta = await current_metadata(frame)
        uid = meta.get('0020,000e')
        if uid and uid != previous_uid:
            return meta
        await asyncio.sleep(0.35)
    raise FetchError('cannot_confirm_canvas_switched_to_selected_card')


def is_exact_ct(meta):
    return (meta.get('0008,0060') == 'CT' and meta.get('0008,0016') == '1.2.840.10008.5.1.4.1.1.2'
            and bool(re.fullmatch(r'\d+(?:\.\d+)+', meta.get('0020,000e', ''))))


def candidate_cards(entries, args):
    return [x for x in entries if (args.item is None or x['item'] == args.item)
            and (args.frames is None or args.frames in x['numeric_labels'])]


def summarize_metadata(item, meta):
    number = meta.get('0020,0011', '')
    number = int(number) if re.fullmatch(r'\d{1,7}', number) else None
    labels = item['numeric_labels']
    counts = labels if len(labels) == 1 else [n for n in labels if n != number]
    count = counts[0] if len(counts) == 1 else None
    image_type = meta.get('0008,0008', '').upper()
    classic_original = bool(is_exact_ct(meta) and re.search(r'\bORIGINAL\b', image_type) and
                            not re.search(r'LOCALIZER|SCOUT|DERIVED', image_type))
    body = meta.get('0018,0015', '').upper()
    body = body if body in {'HEAD', 'BRAIN', 'NECK', 'CHEST', 'ABDOMEN', 'PELVIS', 'SPINE', 'CSPINE',
                            'TSPINE', 'LSPINE', 'KNEE', 'HIP', 'LEG', 'FOOT', 'ANKLE', 'SHOULDER', 'HAND'} else None
    side = meta.get('0020,0062', meta.get('0020,0060', '')).upper()
    side = side if side in {'L', 'R', 'B', 'U'} else None
    return {'item': item['item'], 'frames': count, 'series_number': number,
            'classic_original_ct': classic_original, 'body_part': body, 'laterality': side}


async def inspect_catalog(frame, cards, entries, args):
    candidates = candidate_cards(entries, args)
    if len(candidates) > 30:
        raise FetchError('narrow_catalog_with_frames_or_item')
    previous = None
    for item in candidates:
        meta = await activate(frame, cards, item['item'], previous)
        previous = meta['0020,000e']
        note(stage='catalog_item_checked', **summarize_metadata(item, meta))


async def choose(frame, cards, entries, args):
    candidates = candidate_cards(entries, args)
    matches = []
    if not candidates:
        raise FetchError('no_series_matches_visible_catalog')
    if len(candidates) > 30:
        raise FetchError('narrow_catalog_with_frames_or_item')
    previous = None
    for item in candidates:
        meta = await activate(frame, cards, item['item'], previous)
        previous = meta['0020,000e']
        info = summarize_metadata(item, meta)
        series, count = info['series_number'], info['frames']
        if series is None:
            raise FetchError('candidate_series_number_unresolved')
        if args.series_number != series:
            continue
        if count is None:
            raise FetchError('target_viewer_frame_count_unresolved')
        if count < 2 or not info['classic_original_ct']:
            continue
        if args.frames is not None and count != args.frames:
            continue
        matches.append((item['item'], count, series, meta['0020,000e']))
    if not matches:
        raise FetchError('no_confirmed_classic_ct_series')
    if len(matches) != 1:
        note(stage='selection_ambiguous', candidates=[{'item': a, 'frames': b, 'series_number': c} for a,b,c,_ in matches])
        raise FetchError('specify_one_series')
    card, count, number, uid = matches[0]
    if candidates[-1]['item'] != card:
        await activate(frame, cards, card, previous)
    final = await current_metadata(frame)
    if final.get('0020,000e') != uid:
        raise FetchError('selected_series_changed')
    note(stage='target_series_verified', item=card, series_number=number, viewer_frames=count)
    return count, number, uid


def known_non_original(meta):
    image_type = meta.get('0008,0008', '').upper()
    sop = meta.get('0008,0016', '')
    # Secondary Capture storage family and explicitly marked localizers/derived images.
    return bool(re.search(r'\b(?:DERIVED|LOCALIZER|SCOUT)\b', image_type)
                or re.fullmatch(r'1\.2\.840\.10008\.5\.1\.4\.1\.1\.7(?:\.[1-4])?', sop))


def assess_anatomic_candidate(meta, info, target, detail=None):
    clue = anatomy.classify(meta)
    relevant = anatomy.relevance(clue, target)
    if known_non_original(meta):
        return clue, 'excluded_non_original'
    if relevant == 'exclude':
        return clue, 'excluded_other_anatomy'
    if info['classic_original_ct'] and clue['transverse'] is False:
        return clue, 'excluded_non_transverse'
    if (relevant != 'match' or not info['classic_original_ct'] or clue['transverse'] is not True
            or not info['series_number'] or not info['frames'] or info['frames'] < 2):
        return clue, 'uncertain'
    if detail is None or clue['kernel_detail'] == detail:
        return clue, 'match'
    if clue['kernel_detail'] in {'sharp', 'soft'}:
        return clue, 'excluded_other_detail'
    return clue, 'uncertain'


def resolve_anatomic(matches, unresolved):
    if unresolved:
        raise FetchError('plausible_series_has_unresolved_anatomy_or_type')
    if not matches:
        raise FetchError('no_confident_anatomic_match')
    if len(matches) != 1:
        raise FetchError('multiple_anatomic_matches_need_reconstruction_intent')
    return matches[0]


async def choose_by_anatomy(frame, cards, entries, args, *, select):
    # Scan the visible series without a caller-supplied number, count, card index, or local baseline.
    candidates = [entry for entry in entries if entry['numeric_labels'] != [1]]
    if not candidates or len(candidates) > 30:
        raise FetchError('unsupported_anatomic_catalog_size')
    matches, unresolved = [], []
    previous = None
    for entry in candidates:
        try:
            meta = await activate(frame, cards, entry['item'], previous)
        except FetchError as exc:
            if str(exc) != 'cannot_confirm_canvas_switched_to_selected_card':
                raise
            unresolved.append(entry['item'])
            note(stage='anatomic_candidate_unresolved', item=entry['item'])
            previous = None
            continue
        previous = meta['0020,000e']
        info = summarize_metadata(entry, meta)
        clue, verdict = assess_anatomic_candidate(meta, info, args.target_spec, args.detail)
        note(stage='anatomic_candidate', **info, **clue, verdict=verdict)
        if verdict == 'uncertain':
            unresolved.append(entry['item'])
        if verdict == 'match':
            matches.append((info, previous))
    note(stage='anatomic_selection_summary', matching_series=[info['series_number'] for info, _ in matches],
         unresolved_cards=unresolved)
    if not select:
        return
    info, uid = resolve_anatomic(matches, unresolved)
    if info['item'] != candidates[-1]['item']:
        await activate(frame, cards, info['item'], previous)
    final = await current_metadata(frame)
    if final.get('0020,000e') != uid:
        raise FetchError('selected_series_changed')
    note(stage='target_series_verified', selection='anatomy', item=info['item'],
         series_number=info['series_number'], viewer_frames=info['frames'])
    return info['frames'], info['series_number'], uid


async def export_web(page, frame, target, timeout):
    check_frame(frame)
    canvas = frame.locator('canvas.cornerstone-canvas').filter(visible=True)
    if await canvas.count() != 1:
        raise FetchError('requires_one_active_2d_canvas')
    # The viewer uses an overlay on top of the actual canvas. Target the visible
    # interactive viewport containing it, which receives the normal context click.
    viewport = frame.locator('.image-view-container:has(canvas.cornerstone-canvas)').filter(visible=True)
    if await viewport.count() != 1:
        raise FetchError('unsupported_2d_canvas_container')
    await viewport.click(button='right')
    note(stage='viewer_context_opened')
    export = frame.get_by_text(re.compile(r'^导出此序列\s*dicom\s*图像$', re.I)).filter(visible=True)
    await export.wait_for(state='visible', timeout=8000)
    check_frame(frame)
    note(stage='official_export_menu_visible')
    async with page.expect_download(timeout=timeout * 1000) as event:
        await export.click()
        note(stage='official_export_clicked')
    download = await event.value
    # Website filenames may include patient information. Save to a neutral local name.
    await download.save_as(target)
    note(stage='browser_zip_saved', file_bytes=target.stat().st_size)


def validate_archive(path, expected_count, expected_num, expected_uid, target=None, detail=None):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        import ct_review
        with ZipFile(path) as z:
            if z.testzip() is not None:
                raise FetchError('invalid_zip_crc')
        with ct_review.source_items(path) as (items, opener):
            groups, skipped = ct_review.scan(items, opener)
            if skipped or len(groups) != 1:
                raise FetchError('zip_does_not_contain_one_clean_ct_series')
            first = groups[0][0][1]
            if int(first.SeriesNumber) != expected_num or str(first.SeriesInstanceUID) != expected_uid:
                raise FetchError('zip_series_identity_differs_from_viewer')
            geometry, ordered = ct_review.geometry(groups[0])
            if len(ordered) != expected_count:
                raise FetchError('zip_unique_count_differs_from_viewer')
            if target:
                for _, header in ordered:
                    meta = anatomy.header_metadata(header)
                    clue = anatomy.classify(meta)
                    if (anatomy.relevance(clue, target) != 'match' or clue['transverse'] is not True
                            or 'ORIGINAL' not in meta.get('0008,0008', '').upper().split('\\')
                            or known_non_original(meta) or (detail and clue['kernel_detail'] != detail)):
                        raise FetchError('zip_anatomy_or_reconstruction_inconsistent')
            _, hu, _ = ct_review.decode(ordered, opener, geometry['shape_krc'])
    return {'decoded_slices': len(ordered), 'identity_matches_viewer': True,
            'slice_center_spacing_mm': geometry['slice_center_spacing_mm'],
            'pixel_spacing_row_col_mm': geometry['pixel_spacing_row_col_mm'],
            'units': 'HU' if hu else 'rescaled unit unverified'}


def installed_browser_args(channel):
    if sys.platform != 'win32':
        return {'channel': channel}
    fragment = ('Google/Chrome/Application/chrome.exe' if channel == 'chrome'
                else 'Microsoft/Edge/Application/msedge.exe')
    roots = [os.environ.get('ProgramW6432'), os.environ.get('ProgramFiles'),
             os.environ.get('ProgramFiles(x86)'), 'C:/Program Files', 'C:/Program Files (x86)']
    for root in roots:
        if root:
            executable = Path(root) / fragment
            if executable.is_file():
                return {'channel': channel, 'executable_path': str(executable)}
    return {'channel': channel}


async def run(args, url, code):
    from playwright.async_api import async_playwright
    output = args.out.resolve() if args.out else None
    if output and (output.suffix.lower() != '.zip' or output.exists() or not output.parent.is_dir()):
        raise FetchError('output_must_be_new_zip_in_existing_private_folder')
    temp_parent = output.parent if output else args.work.resolve()
    with TemporaryDirectory(prefix='myfilm-', dir=temp_parent) as tmp:
        staged = Path(tmp) / 'series.zip'
        temp_vars = ('TMP', 'TEMP', 'TMPDIR')
        debug_vars = ('DEBUG', 'PWDEBUG', 'DEBUG_FILE')
        previous = {key: os.environ.get(key) for key in temp_vars + debug_vars}
        os.environ.update({key: tmp for key in temp_vars})
        for key in debug_vars:
            os.environ.pop(key, None)
        try:
            async with async_playwright() as p:
                note(stage='browser_driver_ready')
                b = await p.chromium.launch(**installed_browser_args(args.browser), headless=True, chromium_sandbox=True,
                                            downloads_path=tmp, timeout=25000)
                note(stage='isolated_browser_started')
                try:
                    context = await b.new_context(accept_downloads=True, locale='zh-CN',
                                                  viewport={'width': 1500, 'height': 940})
                    context.set_default_timeout(12000)
                    page = await context.new_page()
                    network_errors = {}
                    def request_failed(request):
                        failure = re.search(r'\bERR_[A-Z_]+\b', request.failure or '')
                        if failure:
                            key = failure.group()
                            network_errors[key] = network_errors.get(key, 0) + 1
                    page.on('requestfailed', request_failed)
                    try:
                        frame, cards = await enter_viewer(page, url, code)
                    except Exception:
                        if network_errors:
                            note(stage='viewer_browser_network_status', error_counts=network_errors)
                        raise
                    entries = await catalog(cards)
                    note(stage='series_catalog', series=entries)
                    if args.command == 'catalog':
                        if args.target_spec:
                            await choose_by_anatomy(frame, cards, entries, args, select=False)
                        elif args.inspect:
                            await inspect_catalog(frame, cards, entries, args)
                        return
                    if args.target_spec:
                        count, number, uid = await choose_by_anatomy(frame, cards, entries, args, select=True)
                    else:
                        count, number, uid = await choose(frame, cards, entries, args)
                    await export_web(page, frame, staged, args.export_timeout)
                finally:
                    await b.close()
        finally:
            for key, val in previous.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
        validation = validate_archive(staged, count, number, uid, args.target_spec, args.detail)
        os.link(staged, output)  # Same filesystem; atomic publication, without overwriting a path created meanwhile.
        note(stage='validated_dicom_zip', series_number=number, viewer_frames=count, **validation)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Never echo argument values (in particular credentials supplied on the command line by mistake).
        self.exit(2, '{"stage": "failed", "reason": "invalid_cli_arguments_check_help"}\n')


def main():
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['catalog', 'fetch'])
    parser.add_argument('--input-stdin', action='store_true', help='Read one private JSON line containing url and code.')
    parser.add_argument('--browser', choices=['chrome', 'msedge'], default='chrome')
    parser.add_argument('--work', type=Path, help='Private, existing directory for catalog browser temporary files.')
    parser.add_argument('--inspect', action='store_true', help='For catalog, read minimal safe series metadata.')
    parser.add_argument('--out', type=Path, help='New private ZIP path for fetch.')
    parser.add_argument('--series-number', type=int)
    parser.add_argument('--target', help='Select by anatomy: 左膝, 右膝, or 腰椎.')
    parser.add_argument('--detail', choices=['sharp', 'soft'], help='Optional explicit reconstruction detail for a knee target.')
    parser.add_argument('--frames', type=int)
    parser.add_argument('--item', type=int)
    parser.add_argument('--export-timeout', type=int, default=240)
    args = parser.parse_args()
    if (args.command == 'catalog' and (not args.work or not args.work.is_dir())) or (args.command == 'fetch' and not args.out):
        parser.error('catalog requires --work; fetch requires --out')
    args.target_spec = anatomy.target_from_text(args.target) if args.target else None
    if args.target and not args.target_spec:
        note(stage='failed', reason='anatomic_target_not_supported')
        return 2
    if args.target_spec and any(x is not None for x in (args.series_number, args.frames, args.item)):
        parser.error('do not add numbered selectors to an anatomic request')
    if args.detail and (not args.target_spec or args.target_spec.region != 'knee'):
        parser.error('reconstruction detail requires a knee target')
    if args.command == 'fetch' and args.series_number is None and not args.target_spec:
        note(stage='failed', reason='fetch_requires_supported_anatomic_target_or_explicit_series_number')
        return 2
    if not 30 <= args.export_timeout <= 900:
        parser.error('export timeout must be 30 to 900 seconds')
    try:
        url, code = share_input(args.input_stdin)
        asyncio.run(asyncio.wait_for(run(args, url, code), timeout=args.export_timeout + 170))
    except FetchError as exc:
        note(stage='failed', reason=str(exc))
        return 2
    except Exception as exc:
        # Playwright errors often echo URLs and markup. Emit only type and the preceding safe stage.
        error_text = str(exc)
        api = re.match(r'^([A-Za-z]+\.[a-z_]+):', error_text)
        browser_code = re.search(r'\b(?:ERR_[A-Z_]+|EACCES|EPERM|ENOENT)\b', error_text)
        note(stage='failed', reason=type(exc).__name__, api=api.group(1) if api else None,
             browser_code=browser_code.group() if browser_code else None,
             pointer_interception='intercepts pointer events' in error_text)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
