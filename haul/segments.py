"""Turns aria2's piece bitfield into the cells of the segment map."""


def cells(bitfield, pieces, count):
    """Fraction complete (0 to 1) of each of `count` cells. Pieces are spread evenly over them.

    `bitfield` is aria2's hex string: the high bit of the first digit is piece 0.
    """
    if not bitfield or pieces <= 0 or count <= 0:
        return []
    count = min(count, pieces)
    done, total = [0] * count, [0] * count
    for piece in range(pieces):
        digit = piece // 4
        cell = piece * count // pieces
        total[cell] += 1
        if digit < len(bitfield) and int(bitfield[digit], 16) >> (3 - piece % 4) & 1:
            done[cell] += 1
    return [d / t for d, t in zip(done, total, strict=True)]
