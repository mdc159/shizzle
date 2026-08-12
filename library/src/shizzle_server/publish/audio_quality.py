"""Offline measurements for the decoded six-stem default browser mix."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

DEFAULT_MASTER_HEADROOM_DB = -3.0
TRUE_PEAK_LIMIT_DBTP = -1.0


@dataclass(frozen=True)
class MixQuality:
    integrated_lufs: float
    loudness_range_lu: float
    true_peak_dbtp: float
    sample_peak_dbfs: float
    min_sample: float
    max_sample: float
    nan_samples: int
    infinite_samples: int
    additional_common_gain_db: float
    sample_clipping: bool
    passed_pre_limiter: bool

    def as_dict(self) -> dict[str, float | int | bool]:
        return asdict(self)


def build_six_stem_mix_filter(
    gain_dbs: list[float], *, master_headroom_db: float = DEFAULT_MASTER_HEADROOM_DB
) -> str:
    if len(gain_dbs) != 6:
        raise ValueError("exactly six stem gains are required")
    inputs = []
    chains = []
    for index, gain_db in enumerate(gain_dbs):
        label = f"a{index}"
        chains.append(f"[{index}:a]volume={gain_db:.6f}dB[{label}]")
        inputs.append(f"[{label}]")
    chains.append(
        f"{''.join(inputs)}amix=inputs=6:duration=longest:dropout_transition=0:normalize=0,"
        f"volume={master_headroom_db:.6f}dB,asplit=2[ebu][stats]"
    )
    chains.append("[ebu]ebur128=peak=true[out_ebu]")
    chains.append("[stats]astats=metadata=0:reset=0[out_stats]")
    return ";".join(chains)


def _required(pattern: str, text: str, label: str) -> str:
    matches = re.findall(pattern, text, flags=re.MULTILINE)
    if not matches:
        raise ValueError(f"ffmpeg quality output is missing {label}")
    return str(matches[-1])


def parse_mix_quality(stderr: str) -> MixQuality:
    summary_index = stderr.rfind("Summary:")
    if summary_index < 0:
        raise ValueError("ffmpeg quality output is missing EBU summary")
    ebu = stderr[summary_index:]
    overall_index = stderr.rfind("Overall")
    if overall_index < 0:
        raise ValueError("ffmpeg quality output is missing astats Overall")
    overall = stderr[overall_index:summary_index]

    integrated = float(_required(r"^\s*I:\s*([-+\d.]+)\s+LUFS", ebu, "integrated loudness"))
    lra = float(_required(r"^\s*LRA:\s*([-+\d.]+)\s+LU", ebu, "loudness range"))
    true_peak = float(_required(r"^\s*Peak:\s*([-+\d.]+)\s+dBFS", ebu, "true peak"))
    sample_peak = float(_required(r"Peak level dB:\s*([-+\d.]+)", overall, "sample peak"))
    min_sample = float(_required(r"Min level:\s*([-+\d.]+)", overall, "minimum sample"))
    max_sample = float(_required(r"Max level:\s*([-+\d.]+)", overall, "maximum sample"))
    nan_samples = int(float(_required(r"Number of NaNs:\s*([-+\d.]+)", overall, "NaN count")))
    infinite_samples = int(
        float(_required(r"Number of Infs:\s*([-+\d.]+)", overall, "infinite count"))
    )
    clipping = sample_peak >= 0 or min_sample < -1 or max_sample > 1
    additional_gain = min(0.0, TRUE_PEAK_LIMIT_DBTP - true_peak)
    passed = (
        not clipping
        and true_peak <= TRUE_PEAK_LIMIT_DBTP
        and nan_samples == 0
        and infinite_samples == 0
    )
    return MixQuality(
        integrated_lufs=integrated,
        loudness_range_lu=lra,
        true_peak_dbtp=true_peak,
        sample_peak_dbfs=sample_peak,
        min_sample=min_sample,
        max_sample=max_sample,
        nan_samples=nan_samples,
        infinite_samples=infinite_samples,
        additional_common_gain_db=round(additional_gain, 3),
        sample_clipping=clipping,
        passed_pre_limiter=passed,
    )
