"""Opt-in end-of-stroke pen-lift overlap for AxiDraw motion lists."""

from __future__ import annotations

import copy


MM_PER_INCH = 25.4
MIN_MOVE_INCH = 1e-9


def _split_sm(move, fraction: float, start_x: float, start_y: float):
    """Split one AxiDraw SM move while preserving exact steps and total time."""
    fraction = max(0.0, min(1.0, float(fraction)))
    steps_2, steps_1, duration_ms = move[1]
    end_data = list(move[2])
    end_x, end_y = float(end_data[0]), float(end_data[1])
    distance = float(end_data[3])

    if duration_ms < 2 or fraction <= 0.0 or fraction >= 1.0:
        return None

    first_duration = max(1, min(duration_ms - 1, int(round(duration_ms * fraction))))
    first_steps_2 = int(round(steps_2 * fraction))
    first_steps_1 = int(round(steps_1 * fraction))

    first_data = copy.copy(end_data)
    first_data[0] = start_x + (end_x - start_x) * fraction
    first_data[1] = start_y + (end_y - start_y) * fraction
    first_data[3] = distance * fraction

    second_data = copy.copy(end_data)
    second_data[3] = distance - first_data[3]

    first = [
        "SM",
        (first_steps_2, first_steps_1, first_duration),
        first_data,
    ]
    second = [
        "SM",
        (
            steps_2 - first_steps_2,
            steps_1 - first_steps_1,
            duration_ms - first_duration,
        ),
        second_data,
    ]
    return first, second


def overlap_final_lift(move_list, start_x: float, start_y: float, soft_out_mm: float):
    """Insert a non-blocking lift before the final portion of a pen-down path.

    The overlap is capped at half of the path length so very short marks retain
    a definite fully-down section. XY steps, duration, endpoint, and total
    geometric distance are preserved exactly.
    """
    requested_inch = max(0.0, float(soft_out_mm)) / MM_PER_INCH
    if requested_inch <= MIN_MOVE_INCH:
        return list(move_list), False

    total_distance = sum(
        max(0.0, float(move[2][3]))
        for move in move_list
        if move and move[0] == "SM" and move[2]
    )
    if total_distance <= MIN_MOVE_INCH:
        return list(move_list), False

    overlap_distance = min(requested_inch, total_distance * 0.5)
    lift_at = total_distance - overlap_distance
    output = []
    travelled = 0.0
    previous_x = float(start_x)
    previous_y = float(start_y)
    inserted = False

    for move in move_list:
        if move[0] != "SM" or inserted:
            output.append(move)
            if move[0] == "SM" and move[2]:
                previous_x = float(move[2][0])
                previous_y = float(move[2][1])
            continue

        distance = max(0.0, float(move[2][3]))
        move_end = travelled + distance
        if move_end + MIN_MOVE_INCH < lift_at:
            output.append(move)
            travelled = move_end
            previous_x = float(move[2][0])
            previous_y = float(move[2][1])
            continue

        before_distance = max(0.0, lift_at - travelled)
        fraction = before_distance / distance if distance > MIN_MOVE_INCH else 0.0
        split = _split_sm(move, fraction, previous_x, previous_y)
        if split is not None:
            first, second = split
            output.append(first)
            output.append(["soft_raise", None])
            output.append(second)
        elif fraction >= 0.5:
            output.append(move)
            output.append(["soft_raise", None])
        else:
            output.append(["soft_raise", None])
            output.append(move)
        inserted = True
        previous_x = float(move[2][0])
        previous_y = float(move[2][1])

    if not inserted:
        return list(move_list), False
    output.append(["soft_raise_finish", None])
    return output, True
