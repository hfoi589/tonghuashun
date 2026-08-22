from __future__ import annotations

from io import BytesIO
from random import Random

from PIL import Image, ImageChops, ImageDraw
import pytest

import level2_service.runner as runner_module
from level2_service.models import CaptureKind


SCREEN_WIDTH = 1080
SCREEN_HEIGHT = 1920
HEADER_HEIGHT = 215
FOOTER_HEIGHT = 154
VIEWPORT_HEIGHT = SCREEN_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _screen(document: Image.Image, offset: int) -> bytes:
    screen = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), (255, 255, 255))
    screen.paste(Image.new("RGB", (SCREEN_WIDTH, HEADER_HEIGHT), (240, 48, 48)), (0, 0))
    screen.paste(document.crop((0, offset, SCREEN_WIDTH, offset + VIEWPORT_HEIGHT)), (0, HEADER_HEIGHT))
    screen.paste(Image.new("RGB", (SCREEN_WIDTH, FOOTER_HEIGHT), (31, 31, 31)), (0, SCREEN_HEIGHT - FOOTER_HEIGHT))
    return _png(screen)


def test_long_capture_stitches_scrolled_content_without_repeating_fixed_bars() -> None:
    """Repeating either fixed bar would turn three frames into a screen collage."""
    stitch_long_capture = getattr(runner_module, "stitch_long_capture")
    offsets = (0, 500, 1320)
    document_height = VIEWPORT_HEIGHT + offsets[-1]
    random = Random(601872)
    row_strip = Image.frombytes("RGB", (1, document_height), random.randbytes(document_height * 3))
    document = row_strip.resize((SCREEN_WIDTH, document_height))

    stitched_png = stitch_long_capture(tuple(_screen(document, offset) for offset in offsets))

    with Image.open(BytesIO(stitched_png)) as stitched:
        result = stitched.convert("RGB").copy()
    assert result.size == (SCREEN_WIDTH, HEADER_HEIGHT + document_height + FOOTER_HEIGHT)
    assert ImageChops.difference(result.crop((0, 0, SCREEN_WIDTH, HEADER_HEIGHT)), Image.new("RGB", (SCREEN_WIDTH, HEADER_HEIGHT), (240, 48, 48))).getbbox() is None
    assert ImageChops.difference(result.crop((0, HEADER_HEIGHT, SCREEN_WIDTH, HEADER_HEIGHT + document_height)), document).getbbox() is None
    assert ImageChops.difference(result.crop((0, result.height - FOOTER_HEIGHT, SCREEN_WIDTH, result.height)), Image.new("RGB", (SCREEN_WIDTH, FOOTER_HEIGHT), (31, 31, 31))).getbbox() is None


def test_long_capture_keeps_the_bottom_of_a_chart_after_large_swipes() -> None:
    """A real 1259px swipe must not be underestimated and overwrite the amount chart bottom."""
    stitch_long_capture = getattr(runner_module, "stitch_long_capture")
    offsets = (0, 1259, 2534)
    document_height = VIEWPORT_HEIGHT + offsets[-1]
    random = Random(33015)
    row_strip = Image.frombytes("RGB", (1, document_height), random.randbytes(document_height * 3))
    document = row_strip.resize((SCREEN_WIDTH, document_height))

    stitched_png = stitch_long_capture(tuple(_screen(document, offset) for offset in offsets))

    with Image.open(BytesIO(stitched_png)) as stitched:
        result = stitched.convert("RGB").copy()
    assert result.size == (SCREEN_WIDTH, HEADER_HEIGHT + document_height + FOOTER_HEIGHT)
    assert ImageChops.difference(
        result.crop((0, HEADER_HEIGHT, SCREEN_WIDTH, HEADER_HEIGHT + document_height)),
        document,
    ).getbbox() is None


def test_long_capture_keeps_a_chart_that_renders_only_in_the_newer_overlap() -> None:
    """Keeping the older overlap drops a lazily rendered fourth chart from the result."""
    stitch_long_capture = getattr(runner_module, "stitch_long_capture")
    random = Random(33015)
    document_height = VIEWPORT_HEIGHT + 500
    row_strip = Image.frombytes("RGB", (1, document_height), random.randbytes(document_height * 3))
    hidden_document = row_strip.resize((SCREEN_WIDTH, document_height))
    draw = ImageDraw.Draw(hidden_document)
    chart_box = (20, 1200, 700, 1450)
    draw.rectangle(chart_box, fill="white")
    rendered_document = hidden_document.copy()
    ImageDraw.Draw(rendered_document).rectangle(chart_box, fill=(245, 155, 0))

    stitched_png = stitch_long_capture(
        (
            _screen(hidden_document, 0),
            _screen(rendered_document, 500),
        )
    )

    with Image.open(BytesIO(stitched_png)) as stitched:
        result = stitched.convert("RGB").copy()
    assert result.getpixel((100, HEADER_HEIGHT + 1300)) == (245, 155, 0)


def test_long_capture_aligns_on_the_quote_column_when_chart_canvas_repaints() -> None:
    """The self-drawn chart column can repaint while the quote list still gives the true offset."""
    stitch_long_capture = getattr(runner_module, "stitch_long_capture")
    random = Random(601872)
    document_height = VIEWPORT_HEIGHT + 500
    document = Image.frombytes(
        "RGB",
        (SCREEN_WIDTH, document_height),
        random.randbytes(SCREEN_WIDTH * document_height * 3),
    )
    first = _screen(document, 0)
    with Image.open(BytesIO(_screen(document, 500))) as decoded:
        repainted = decoded.convert("RGB").copy()
    ImageDraw.Draw(repainted).rectangle(
        (0, HEADER_HEIGHT, 735, SCREEN_HEIGHT - FOOTER_HEIGHT),
        fill=(245, 155, 0),
    )

    stitched_png = stitch_long_capture((first, _png(repainted)))

    with Image.open(BytesIO(stitched_png)) as stitched:
        assert stitched.size == (SCREEN_WIDTH, HEADER_HEIGHT + document_height + FOOTER_HEIGHT)


def test_long_capture_rejects_a_swipe_that_did_not_move_the_page() -> None:
    """A stuck page must not be published as a repeated or falsely complete long image."""
    stitch_long_capture = getattr(runner_module, "stitch_long_capture")
    random = Random(42)
    document = Image.frombytes(
        "RGB",
        (1, VIEWPORT_HEIGHT),
        random.randbytes(VIEWPORT_HEIGHT * 3),
    ).resize((SCREEN_WIDTH, VIEWPORT_HEIGHT))
    frame = _screen(document, 0)

    with pytest.raises(runner_module.NavigationError, match="did not scroll"):
        stitch_long_capture((frame, frame, frame))


def test_indicator_reader_returns_validated_current_values_for_all_three_charts() -> None:
    """Returning an unvalidated OCR blob would expose labels or unrelated quote values."""
    reader_type = getattr(runner_module, "IndicatorValueReader")
    recognized = {
        CaptureKind.RETAIL_COUNT: "散户数量：21.23",
        CaptureKind.LARGE_ORDER_NET: "净量: -0.02 现手:12736",
        CaptureKind.LARGE_ORDER_AMOUNT: "金额:-2802.6万",
    }
    random = Random(7)
    document = Image.frombytes(
        "RGB",
        (1, VIEWPORT_HEIGHT),
        random.randbytes(VIEWPORT_HEIGHT * 3),
    ).resize((SCREEN_WIDTH, VIEWPORT_HEIGHT))
    frame = _screen(document, 0)

    values = reader_type(ocr=lambda _crop, kind: recognized[kind]).read((frame, frame, frame))

    assert values == {
        CaptureKind.LARGE_ORDER_NET: "-0.02",
        CaptureKind.LARGE_ORDER_AMOUNT: "-2802.6万",
        CaptureKind.RETAIL_COUNT: "21.23",
    }


def test_indicator_reader_does_not_guess_a_missing_amount_unit() -> None:
    """A bare amount number is ambiguous between yuan, ten-thousands, and hundred-millions."""
    reader_type = getattr(runner_module, "IndicatorValueReader")

    assert reader_type._validated_value(CaptureKind.LARGE_ORDER_AMOUNT, "金额:-2802.6") is None


def test_indicator_reader_uses_calibrated_value_only_crops() -> None:
    """Including chart labels and grid lines makes Tesseract corrupt signs and decimals."""
    reader_type = getattr(runner_module, "IndicatorValueReader")
    seen: dict[CaptureKind, tuple[tuple[int, int], str]] = {}
    recognized = {
        CaptureKind.RETAIL_COUNT: "21.23",
        CaptureKind.LARGE_ORDER_NET: "-0.02",
        CaptureKind.LARGE_ORDER_AMOUNT: "-2802.6万",
    }

    def inspect_crop(raw: bytes, kind: CaptureKind) -> str:
        with Image.open(BytesIO(raw)) as image:
            seen[kind] = (image.size, image.mode)
        return recognized[kind]

    blank = _png(Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "white"))
    reader_type(ocr=inspect_crop).read((blank, blank, blank))

    assert seen == {
        CaptureKind.RETAIL_COUNT: ((600, 280), "L"),
        CaptureKind.LARGE_ORDER_NET: ((560, 300), "RGB"),
        CaptureKind.LARGE_ORDER_AMOUNT: ((980, 400), "L"),
    }


def test_indicator_reader_tracks_values_when_the_second_swipe_moves_farther() -> None:
    """The final swipe distance varies, so fixed Y coordinates miss the net and amount rows."""
    reader_type = getattr(runner_module, "IndicatorValueReader")
    final_frame = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "white")
    draw = ImageDraw.Draw(final_frame)
    draw.rectangle((350, 450, 455, 482), fill=(35, 160, 40))
    draw.rectangle((320, 760, 520, 802), fill=(245, 155, 0))
    frames = (
        _png(Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "white")),
        _png(Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "white")),
        _png(final_frame),
    )
    non_white: dict[CaptureKind, bool] = {}

    def inspect_crop(raw: bytes, kind: CaptureKind) -> str:
        with Image.open(BytesIO(raw)) as image:
            non_white[kind] = ImageChops.difference(image.convert("RGB"), Image.new("RGB", image.size, "white")).getbbox() is not None
        return {
            CaptureKind.RETAIL_COUNT: "21.23",
            CaptureKind.LARGE_ORDER_NET: "-0.02",
            CaptureKind.LARGE_ORDER_AMOUNT: "-2802.6万",
        }[kind]

    reader_type(ocr=inspect_crop).read(frames)

    assert non_white[CaptureKind.LARGE_ORDER_NET] is True
    assert non_white[CaptureKind.LARGE_ORDER_AMOUNT] is True


def test_indicator_reader_uses_the_frame_where_both_large_order_values_are_visible() -> None:
    """The bottom-marker frame contains order-queue colors, not the two chart values."""
    reader_type = getattr(runner_module, "IndicatorValueReader")
    first_frame = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "white")
    values_frame = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "white")
    bottom_frame = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "white")

    values_draw = ImageDraw.Draw(values_frame)
    values_draw.rectangle((350, 450, 455, 482), fill=(35, 160, 40))
    values_draw.rectangle((340, 440, 346, 446), fill=(255, 0, 255))
    values_draw.rectangle((320, 760, 520, 802), fill=(245, 155, 0))
    values_draw.rectangle((305, 720, 312, 730), fill="black")

    bottom_draw = ImageDraw.Draw(bottom_frame)
    bottom_draw.rectangle((350, 1000, 455, 1032), fill=(35, 160, 40))
    bottom_draw.rectangle((320, 300, 520, 342), fill=(245, 155, 0))

    def recognize_only_real_value_crops(raw: bytes, kind: CaptureKind) -> str:
        if kind == CaptureKind.RETAIL_COUNT:
            return "21.23"
        with Image.open(BytesIO(raw)) as image:
            if kind == CaptureKind.LARGE_ORDER_NET:
                has_marker = any(
                    red > 240 and green < 15 and blue > 240
                    for red, green, blue in image.convert("RGB").getdata()
                )
            else:
                grayscale = image.convert("L")
                has_marker = any(
                    grayscale.getpixel((x, y)) == 0
                    for x in range(60)
                    for y in range(grayscale.height)
                )
        if not has_marker:
            return ""
        return "-0.02" if kind == CaptureKind.LARGE_ORDER_NET else "-2802.6万"

    values = reader_type(ocr=recognize_only_real_value_crops).read(
        (_png(first_frame), _png(values_frame), _png(bottom_frame))
    )

    assert values[CaptureKind.LARGE_ORDER_NET] == "-0.02"
    assert values[CaptureKind.LARGE_ORDER_AMOUNT] == "-2802.6万"
