"""Gradual pen-height entry/exit profiles for AxiDraw motion lists."""

from __future__ import annotations

from axidrawinternal.softout import MM_PER_INCH, MIN_MOVE_INCH, _split_sm


def _sm_distance(move) -> float:
    if not move or move[0] != "SM" or not move[2]:
        return 0.0
    return max(0.0, float(move[2][3]))


def _split_at_distances(move_list, start_x, start_y, cuts):
    """Split SM commands at cumulative path distances, preserving all totals."""
    pending_cuts = sorted(
        {float(value) for value in cuts if float(value) > MIN_MOVE_INCH}
    )
    cut_index = 0
    travelled = 0.0
    previous_x = float(start_x)
    previous_y = float(start_y)
    output = []

    for original in move_list:
        if not original or original[0] != "SM":
            output.append(original)
            continue

        remaining = original
        remaining_distance = _sm_distance(remaining)
        while (
            cut_index < len(pending_cuts)
            and pending_cuts[cut_index] < travelled + remaining_distance - MIN_MOVE_INCH
        ):
            cut = pending_cuts[cut_index]
            if cut <= travelled + MIN_MOVE_INCH:
                cut_index += 1
                continue
            fraction = (cut - travelled) / remaining_distance
            split = _split_sm(remaining, fraction, previous_x, previous_y)
            if split is None:
                break
            first, remaining = split
            output.append(first)
            travelled += _sm_distance(first)
            previous_x = float(first[2][0])
            previous_y = float(first[2][1])
            remaining_distance = _sm_distance(remaining)
            cut_index += 1

        output.append(remaining)
        travelled += remaining_distance
        previous_x = float(remaining[2][0])
        previous_y = float(remaining[2][1])
        while (
            cut_index < len(pending_cuts)
            and pending_cuts[cut_index] <= travelled + MIN_MOVE_INCH
        ):
            cut_index += 1

    return output


def gradual_entry_exit(
    move_list,
    start_x: float,
    start_y: float,
    *,
    ramp_mm: float,
    exit_ramp_mm: float | None = None,
    tail_mm: float,
    segment_mm: float,
    pen_up: float,
    pen_down: float,
):
    """Add a linear Z ramp to the beginning and end of a pen-down path.

    XY steps, time, endpoint, and geometric distance are preserved. Very short
    paths retain a middle section by limiting each ramp to 40% of path length.
    """
    requested_ramp = max(0.0, float(ramp_mm)) / MM_PER_INCH
    if requested_ramp <= MIN_MOVE_INCH:
        return list(move_list), False

    total = sum(_sm_distance(move) for move in move_list)
    if total <= MIN_MOVE_INCH:
        return list(move_list), False

    entry_ramp = min(requested_ramp, total * 0.4)
    requested_exit_ramp = max(
        0.0,
        float(ramp_mm if exit_ramp_mm is None else exit_ramp_mm),
    ) / MM_PER_INCH
    exit_ramp = min(requested_exit_ramp, total * 0.4)
    requested_tail = max(0.0, float(tail_mm)) / MM_PER_INCH
    tail = min(requested_tail, exit_ramp * 0.25)
    lift = max(MIN_MOVE_INCH, exit_ramp - tail)
    segment = max(0.1, float(segment_mm)) / MM_PER_INCH
    entry_count = max(4, int(round(entry_ramp / segment)))
    exit_count = max(4, int(round(lift / segment)))
    exit_start = total - exit_ramp
    tail_start = total - tail

    cuts = [entry_ramp * index / entry_count for index in range(1, entry_count + 1)]
    cuts.extend(
        exit_start + lift * index / exit_count
        for index in range(1, exit_count + 1)
    )
    split_moves = _split_at_distances(move_list, start_x, start_y, cuts)

    output = [["profile_begin", None]]
    travelled = 0.0
    last_height = None
    entry_segment = entry_ramp / entry_count
    exit_segment = lift / exit_count

    for move in split_moves:
        if not move or move[0] != "SM":
            output.append(move)
            continue

        height = None
        if travelled < entry_ramp - MIN_MOVE_INCH:
            index = min(entry_count - 1, int((travelled + MIN_MOVE_INCH) / entry_segment))
            ratio = index / (entry_count - 1)
            height = float(pen_up) + (float(pen_down) - float(pen_up)) * ratio
        elif travelled >= exit_start - MIN_MOVE_INCH:
            if travelled >= tail_start - MIN_MOVE_INCH:
                height = float(pen_up)
            else:
                index = min(
                    exit_count - 1,
                    int((travelled - exit_start + MIN_MOVE_INCH) / exit_segment),
                )
                ratio = index / (exit_count - 1)
                height = float(pen_down) + (float(pen_up) - float(pen_down)) * ratio

        if height is not None:
            if last_height is None or abs(height - last_height) > 1e-7:
                output.append(["profile_height", height])
                last_height = height
            output.append(["profile_SM", move[1], move[2]])
        else:
            output.append(move)
        travelled += _sm_distance(move)

    output.append(["profile_finish", None])
    return output, True
