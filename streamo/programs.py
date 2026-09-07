from .control import RuntimeState


def update_bitrate(state: RuntimeState, line: str) -> None:
    if not line.startswith("bitrate="):
        return
    state.set_output_bitrate(parse_bitrate(line.removeprefix("bitrate=").strip()))


def parse_bitrate(value: str) -> float | None:
    if value == "N/A":
        return None
    if value.endswith("Mbits/s"):
        return float(value.removesuffix("Mbits/s").strip()) * 1000
    if value.endswith("kbits/s"):
        return float(value.removesuffix("kbits/s").strip())
    if value.endswith("bits/s"):
        return float(value.removesuffix("bits/s").strip()) / 1000
    return None
