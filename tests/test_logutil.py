from pathlib import Path

from zh_dub import logutil


def test_daily_log_file_and_job_tag(tmp_path: Path):
    logutil.reset()
    logutil.attach(tmp_path)
    logutil.set_job("abc123")
    logutil.detail("ffmpeg -i source.mp4")
    logutil.info("TTS 开始")
    logutil.reset()

    files = list(tmp_path.glob("*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "ffmpeg -i source.mp4" in text
    assert "[abc123]" in text
    assert "INFO  TTS 开始" in text
