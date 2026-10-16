import numpy as np
from PIL import Image

from ssi_annotator import media


def _frame_no(path):
    return int(round(np.asarray(Image.open(path).convert("L"), dtype=float).mean() / 2))


def test_transcode_frame_map_and_extract(tmp_path, numbered_video):
    web = tmp_path / "web.mp4"
    media.transcode_for_web(numbered_video, web)
    info = media.probe(web)
    assert abs(info["fps"] - 30) < 0.01
    times = tuple(media.build_frame_map(web, tmp_path / "map.csv"))
    assert len(times) == 120
    assert times[0] == 0 and times[30] == 1000 and times[-1] == 3967
    assert media.time_to_frame(times, 1000) == 30
    assert media.time_to_frame(times, 1032) == 30
    assert media.time_to_frame(times, 1034) == 31

    out = tmp_path / "frames"
    idxs = media.extract_frames(web, times, 47, 90, 5, out)
    assert idxs == list(range(47, 91, 5))
    files = sorted(out.glob("*.jpg"))
    assert len(files) == len(idxs)
    # 抽出的每张图与全局帧号一致（容许 JPEG 量化误差 ±1）
    for f, i in zip(files, idxs):
        assert abs(_frame_no(f) - i) <= 1, (f.name, i, _frame_no(f))


def test_trim_keeps_frame_alignment(tmp_path, numbered_video):
    trimmed = tmp_path / "trim.mp4"
    media.transcode_for_web(numbered_video, trimmed, start_sec=1.0, end_sec=3.0)
    times = media.build_frame_map(trimmed, tmp_path / "map.csv")
    assert len(times) == 60
    out = tmp_path / "f"
    media.extract_frames(trimmed, tuple(times), 0, 0, 1, out)
    assert abs(_frame_no(next(out.glob("*.jpg"))) - 30) <= 1
