"""GPU type extraction / shorthand resolution (RTX PRO 6000 Blackwell regression)."""
import pytest

from lium.sdk.utils import expand_gpu_shorthand, extract_gpu_type, normalize_gpu_short


@pytest.mark.parametrize(
    "machine_name,expected",
    [
        ("NVIDIA H100 80GB HBM3", "H100"),
        ("NVIDIA H100 PCIe", "H100"),
        ("NVIDIA H200", "H200"),
        ("NVIDIA B200", "B200"),
        ("NVIDIA B300 SXM6 AC", "B300"),
        ("NVIDIA GeForce RTX 4090", "RTX4090"),
        ("NVIDIA GeForce RTX 5090", "RTX5090"),
        ("NVIDIA RTX 6000 Ada Generation", "RTX6000"),
        ("NVIDIA RTX PRO 6000 Blackwell Server Edition", "RTXPRO6000"),
        ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition", "RTXPRO6000"),
        ("NVIDIA RTX A6000", "A6000"),
        ("NVIDIA A100-SXM4-80GB", "A100"),
        ("NVIDIA L40S", "L40S"),
        ("NVIDIA L40", "L40"),
        ("NVIDIA CMP 170HX", "170HX"),
        ("", "Unknown"),
    ],
)
def test_extract_gpu_type(machine_name, expected):
    assert extract_gpu_type(machine_name) == expected


def test_rtx_pro_6000_is_not_reported_as_edition():
    # Regression: the marketplace showed 25 nodes as "1×Edition" because "PRO" broke the RTX match.
    for name in (
        "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
    ):
        assert extract_gpu_type(name) != "Edition"


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("RTXPRO6000", "RTXPRO6000"),
        ("rtxpro6000", "RTXPRO6000"),
        ("RTX PRO 6000", "RTXPRO6000"),
        ("rtx-pro-6000", "RTXPRO6000"),
        ("PRO6000", "RTXPRO6000"),
        ("pro 6000", "RTXPRO6000"),
        ("RTX6000PRO", "RTXPRO6000"),
        ("h100", "H100"),
        ("RTX 4090", "RTX4090"),
    ],
)
def test_normalize_gpu_short(typed, expected):
    assert normalize_gpu_short(typed) == expected


def test_normalized_short_matches_extracted_type():
    # The `--gpu` filter compares these two, so every alias must land on the extracted type.
    machine = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
    for typed in ("RTXPRO6000", "pro6000", "RTX PRO 6000"):
        assert normalize_gpu_short(typed) == extract_gpu_type(machine)
    assert normalize_gpu_short("RTX6000") == extract_gpu_type("NVIDIA RTX 6000 Ada Generation")
    assert normalize_gpu_short("RTX6000") != extract_gpu_type(machine)


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("RTX4090", "RTX 4090"),
        ("RTXPRO6000", "RTX PRO 6000"),
        ("pro6000", "RTX PRO 6000"),
        ("A100", "A100"),
        ("H200", "H200"),
        ("NVIDIA H100 80GB HBM3", "NVIDIA H100 80GB HBM3"),
    ],
)
def test_expand_gpu_shorthand(typed, expected):
    assert expand_gpu_shorthand(typed) == expected
