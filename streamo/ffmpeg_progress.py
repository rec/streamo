from .runtime import RuntimeState


def update_progress(state: RuntimeState, line: str) -> None:
    if line.startswith('out_time_us='):
        value = line.removeprefix('out_time_us=').strip()
        if value.isdecimal():
            state.record_output_progress(int(value))
        return
    if not line.startswith('bitrate='):
        return
    state.set_output_bitrate(parse_bitrate(line.removeprefix('bitrate=').strip()))


def parse_bitrate(value: str) -> float | None:
    if value == 'N/A':
        return None
    if value.endswith('Mbits/s'):
        return float(value.removesuffix('Mbits/s').strip()) * 1000
    if value.endswith('kbits/s'):
        return float(value.removesuffix('kbits/s').strip())
    if value.endswith('bits/s'):
        return float(value.removesuffix('bits/s').strip()) / 1000
    return None
