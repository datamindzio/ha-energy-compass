"""Pure-Python H3 resolution-6 cell index (T-500, ADR-0022, owner decision T-519 = A).

The `h3` package ships no wheel for Home Assistant on Raspberry Pi (Alpine/aarch64) and
needs a C compiler there, which would take the whole integration down. Only one function
is needed, so this is a port of `latLngToCell` of the H3 4.x C library for resolution 6
(a hexagon of roughly 36 km²), with no dependencies. Cells are checked against the
official library in `tests/test_atlas_h3_res6.py`.
"""

import math

RESOLUTION = 6

_TWO_PI = 2.0 * math.pi
_EPSILON = 0.0000000000000001
_SQRT7 = 2.6457513110645905905016157536392604257102
_RSIN60 = 1.1547005383792515290182975610039149112953
_INV_RES0_U_GNOMONIC = 2.61803398874989588842

# icosahedron face centres on the unit sphere
_FACE_CENTERS = (
    (0.2199307791404606, 0.6583691780274996, 0.7198475378926182),
    (-0.2139234834501421, 0.1478171829550703, 0.9656017935214205),
    (0.1092625278784797, -0.4811951572873210, 0.8697775121287253),
    (0.7428567301586791, -0.3593941678278028, 0.5648005936517033),
    (0.8112534709140969, 0.3448953237639384, 0.4721387736413930),
    (-0.1055498149613921, 0.9794457296411413, 0.1718874610009365),
    (-0.8075407579970092, 0.1533552485898818, 0.5695261994882688),
    (-0.2846148069787907, -0.8644080972654206, 0.4144792552473539),
    (0.7405621473854482, -0.6673299564565524, -0.0789837646326737),
    (0.8512303986474293, 0.4722343788582681, -0.2289137388687808),
    (-0.7405621473854481, 0.6673299564565524, 0.0789837646326737),
    (-0.8512303986474292, -0.4722343788582682, 0.2289137388687808),
    (0.1055498149613919, -0.9794457296411413, -0.1718874610009365),
    (0.8075407579970092, -0.1533552485898819, -0.5695261994882688),
    (0.2846148069787908, 0.8644080972654204, -0.4144792552473539),
    (-0.7428567301586791, 0.3593941678278027, -0.5648005936517033),
    (-0.8112534709140971, -0.3448953237639382, -0.4721387736413930),
    (-0.2199307791404607, -0.6583691780274996, -0.7198475378926182),
    (0.2139234834501420, -0.1478171829550704, -0.9656017935214205),
    (-0.1092625278784796, 0.4811951572873210, -0.8697775121287253),
)

# azimuth in radians from each face centre to its ijk i-axis (class II)
_FACE_AXIS_AZIMUTH = (
    5.619958268523939882,
    5.760339081714187279,
    0.780213654393430055,
    0.430469363979999913,
    6.130269123335111400,
    2.692877706530642877,
    2.982963003477243874,
    3.532912002790141181,
    3.494305004259568154,
    3.003214169499538391,
    5.930472956509811562,
    0.138378484090254847,
    0.448714947059150361,
    0.158629650112549365,
    5.891865957979238535,
    2.711123289609793325,
    3.294508837434268316,
    3.804819692245439833,
    3.664438879055192436,
    2.361378999196363184,
)

# (face, i, j, k) -> base cell * 8 + number of 60° ccw rotations into its orientation;
# index = face * 27 + i * 9 + j * 3 + k
# fmt: off
_FACE_IJK_BASE_CELLS = (
    128, 144, 192, 264, 240, 259, 393, 387, 403,
    64, 45, 85, 176, 128, 144, 329, 264, 240,
    32, 5, 21, 121, 64, 45, 249, 176, 128,

    16, 48, 112, 80, 88, 139, 193, 187, 203,
    0, 13, 77, 40, 16, 48, 145, 80, 88,
    33, 29, 61, 65, 0, 13, 129, 40, 16,

    56, 168, 304, 72, 152, 275, 113, 163, 291,
    24, 109, 237, 8, 56, 168, 49, 72, 152,
    34, 101, 213, 1, 24, 109, 17, 8, 56,

    208, 336, 464, 232, 344, 499, 305, 379, 515,
    96, 229, 357, 104, 208, 336, 169, 232, 344,
    35, 125, 253, 25, 96, 229, 57, 104, 208,

    248, 328, 392, 352, 424, 491, 465, 523, 603,
    120, 181, 269, 224, 248, 328, 337, 352, 424,
    36, 69, 133, 97, 120, 181, 209, 224, 248,

    400, 384, 395, 256, 243, 267, 195, 147, 131,
    560, 536, 531, 419, 400, 384, 299, 256, 243,
    664, 699, 683, 595, 560, 536, 457, 419, 400,

    200, 184, 195, 136, 91, 83, 115, 51, 19,
    360, 312, 299, 283, 200, 184, 219, 136, 91,
    504, 475, 459, 451, 360, 312, 371, 283, 200,

    288, 160, 115, 272, 155, 75, 307, 171, 59,
    440, 320, 219, 435, 288, 160, 411, 272, 155,
    576, 483, 371, 587, 440, 320, 571, 435, 288,

    512, 376, 307, 496, 347, 235, 467, 339, 211,
    672, 552, 411, 659, 512, 376, 611, 496, 347,
    776, 715, 571, 787, 672, 552, 771, 659, 512,

    600, 520, 467, 488, 427, 355, 395, 331, 251,
    752, 688, 611, 651, 600, 520, 531, 488, 427,
    856, 835, 771, 811, 752, 688, 683, 651, 600,

    456, 472, 507, 592, 627, 635, 667, 739, 763,
    296, 315, 363, 416, 456, 472, 563, 592, 627,
    192, 187, 203, 259, 296, 315, 403, 416, 456,

    368, 480, 579, 448, 547, 643, 507, 619, 723,
    216, 323, 443, 280, 368, 480, 363, 448, 547,
    112, 163, 291, 139, 216, 323, 203, 280, 368,

    568, 712, 779, 584, 731, 827, 579, 707, 843,
    408, 555, 675, 432, 568, 712, 443, 584, 731,
    304, 379, 515, 275, 408, 555, 291, 432, 568,

    768, 832, 859, 784, 883, 923, 779, 891, 955,
    608, 691, 755, 656, 768, 832, 675, 784, 883,
    464, 523, 603, 499, 608, 691, 515, 656, 768,

    680, 696, 667, 808, 819, 803, 859, 899, 915,
    528, 539, 563, 648, 680, 696, 755, 808, 819,
    392, 387, 403, 491, 528, 539, 603, 648, 680,

    760, 736, 664, 632, 624, 595, 505, 475, 459,
    872, 864, 805, 745, 760, 736, 617, 632, 624,
    940, 949, 917, 849, 872, 864, 721, 745, 760,

    720, 616, 504, 640, 544, 451, 577, 483, 371,
    848, 744, 637, 793, 720, 616, 705, 640, 544,
    939, 877, 765, 905, 848, 744, 841, 793, 720,

    840, 704, 576, 824, 728, 587, 777, 715, 571,
    904, 792, 645, 929, 840, 704, 889, 824, 728,
    938, 853, 725, 969, 904, 792, 953, 929, 840,

    952, 888, 776, 920, 880, 787, 857, 835, 771,
    968, 928, 829, 961, 952, 888, 897, 920, 880,
    937, 909, 845, 945, 968, 928, 913, 961, 952,

    912, 896, 856, 800, 816, 811, 665, 699, 683,
    944, 960, 925, 865, 912, 896, 737, 800, 816,
    936, 973, 957, 873, 944, 960, 761, 865, 912,
)
# fmt: on

_PENTAGON_BASE_CELLS = frozenset((4, 14, 24, 38, 49, 58, 63, 72, 83, 97, 107, 117))

# pentagon base cell -> the two faces whose rotation is clockwise instead of ccw
_PENTAGON_CW_OFFSET_FACES = {
    14: (2, 6),
    24: (1, 5),
    38: (3, 7),
    49: (0, 9),
    58: (4, 8),
    63: (11, 15),
    72: (12, 16),
    83: (10, 19),
    97: (13, 17),
    107: (14, 18),
}

_K_AXES_DIGIT = 1
_ROTATE_CCW = {0: 0, 1: 5, 5: 4, 4: 6, 6: 2, 2: 3, 3: 1}
_ROTATE_CW = {digit: ccw for ccw, digit in _ROTATE_CCW.items()}


def _normalize(i: int, j: int, k: int) -> tuple[int, int, int]:
    """Canonical ijk+ form: non-negative, at least one axis zero."""
    if i < 0:
        j, k, i = j - i, k - i, 0
    if j < 0:
        i, k, j = i - j, k - j, 0
    if k < 0:
        i, j, k = i - k, j - k, 0
    low = min(i, j, k)
    return i - low, j - low, k - low


def _nearest_seventh(n: int) -> int:
    """`round(n / 7)`; n / 7 never lies on a .5 tie."""
    return (n + 3) // 7


def _pos_angle(rads: float) -> float:
    angle = rads + _TWO_PI if rads < 0.0 else rads
    if rads >= _TWO_PI:
        angle -= _TWO_PI
    return angle


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float]:
    norm = math.sqrt(_dot(v, v))
    scale = 1.0 / norm if norm > 0.0 else 0.0
    return (v[0] * scale, v[1] * scale, v[2] * scale)


def _azimuth(p1: tuple[float, float, float], p2: tuple[float, float, float]) -> float:
    """Azimuth from p1 to p2 on the unit sphere (p1 is never a pole)."""
    north = _unit((-p1[2] * p1[0], -p1[2] * p1[1], 1.0 - p1[2] * p1[2]))
    east = (
        north[1] * p1[2] - north[2] * p1[1],
        north[2] * p1[0] - north[0] * p1[2],
        north[0] * p1[1] - north[1] * p1[0],
    )
    along = _dot(p2, p1)
    projected = _unit(
        (p2[0] - along * p1[0], p2[1] - along * p1[1], p2[2] - along * p1[2])
    )
    return math.atan2(_dot(projected, east), _dot(projected, north))


def _face_and_hex2d(latitude: float, longitude: float) -> tuple[int, float, float]:
    """Closest icosahedron face and the res-6 hex2d coordinates of the point on it."""
    ring = math.cos(math.radians(latitude))
    point = (
        math.cos(math.radians(longitude)) * ring,
        math.sin(math.radians(longitude)) * ring,
        math.sin(math.radians(latitude)),
    )
    face, sqd = 0, 5.0
    for candidate, center in enumerate(_FACE_CENTERS):
        distance = sum((c - p) ** 2 for c, p in zip(center, point))
        if distance < sqd:
            face, sqd = candidate, distance

    r = math.acos(1 - sqd * 0.5)
    if r < _EPSILON:
        return face, 0.0, 0.0
    theta = _pos_angle(
        _FACE_AXIS_AZIMUTH[face] - _pos_angle(_azimuth(_FACE_CENTERS[face], point))
    )
    r = math.tan(r) * _INV_RES0_U_GNOMONIC
    for _ in range(RESOLUTION):
        r *= _SQRT7
    return face, r * math.cos(theta), r * math.sin(theta)


def _hex2d_to_ijk(x: float, y: float) -> tuple[int, int, int]:
    """Quantise hex2d coordinates to the containing hexagon's ijk+ address."""
    x2 = abs(y) * _RSIN60
    x1 = abs(x) + x2 / 2.0
    m1, m2 = int(x1), int(x2)
    r1, r2 = x1 - m1, x2 - m2

    if r1 < 0.5:
        if r1 < 1.0 / 3.0:
            i = m1
            j = m2 if r2 < (1.0 + r1) / 2.0 else m2 + 1
        else:
            j = m2 if r2 < 1.0 - r1 else m2 + 1
            i = m1 + 1 if (1.0 - r1) <= r2 < 2.0 * r1 else m1
    elif r1 < 2.0 / 3.0:
        j = m2 if r2 < 1.0 - r1 else m2 + 1
        i = m1 if (2.0 * r1 - 1.0) < r2 < (1.0 - r1) else m1 + 1
    else:
        i = m1 + 1
        j = m2 if r2 < r1 / 2.0 else m2 + 1

    # fold across the axes
    if x < 0.0:
        if j % 2 == 0:
            i = int(i - 2.0 * (i - j // 2))
        else:
            i = int(i - (2.0 * (i - (j + 1) // 2) + 1))
    if y < 0.0:
        i = i - (2 * j + 1) // 2
        j = -j
    return _normalize(i, j, 0)


def _digits_and_base_ijk(
    i: int, j: int, k: int
) -> tuple[list[int], tuple[int, int, int]]:
    """Walk res 6 -> 0: the index digits (res 1..6) and the base cell's ijk."""
    digits = [0] * (RESOLUTION + 1)  # digits[r] for r = 1..RESOLUTION
    for r in range(RESOLUTION - 1, -1, -1):
        last = (i, j, k)
        a, b = i - k, j - k
        if (r + 1) % 2:  # class III: counter-clockwise aperture 7
            i, j, k = _normalize(
                _nearest_seventh(3 * a - b), _nearest_seventh(a + 2 * b), 0
            )
            center = _normalize(3 * i + j, 3 * j + k, i + 3 * k)
        else:  # class II: clockwise aperture 7
            i, j, k = _normalize(
                _nearest_seventh(2 * a + b), _nearest_seventh(3 * b - a), 0
            )
            center = _normalize(3 * i + k, i + 3 * j, j + 3 * k)
        di, dj, dk = _normalize(
            last[0] - center[0], last[1] - center[1], last[2] - center[2]
        )
        digits[r + 1] = di * 4 + dj * 2 + dk
    return digits, (i, j, k)


def _rotate_digits(digits: list[int], table: dict[int, int]) -> None:
    for r in range(1, len(digits)):
        digits[r] = table[digits[r]]


def _leading_digit(digits: list[int]) -> int:
    return next((d for d in digits[1:] if d), 0)


def _rotate_pentagon_ccw(digits: list[int]) -> None:
    """Rotate about a pentagon centre, skipping the deleted k-axes sequence."""
    found = False
    for r in range(1, len(digits)):
        digits[r] = _ROTATE_CCW[digits[r]]
        if not found and digits[r]:
            found = True
            if _leading_digit(digits) == _K_AXES_DIGIT:
                _rotate_digits(digits, _ROTATE_CCW)


def h3_res6_index(latitude: float, longitude: float) -> str:
    """Resolution-6 H3 cell of a WGS84 point (degrees) as a 15-digit hex string."""
    face, x, y = _face_and_hex2d(latitude, longitude)
    digits, base_ijk = _digits_and_base_ijk(*_hex2d_to_ijk(x, y))
    if max(base_ijk) > 2:
        raise ValueError("point outside the icosahedron grid")

    packed = _FACE_IJK_BASE_CELLS[
        face * 27 + base_ijk[0] * 9 + base_ijk[1] * 3 + base_ijk[2]
    ]
    base_cell, rotations = packed >> 3, packed & 7
    if base_cell in _PENTAGON_BASE_CELLS:
        if _leading_digit(digits) == _K_AXES_DIGIT:
            cw = face in _PENTAGON_CW_OFFSET_FACES.get(base_cell, ())
            _rotate_digits(digits, _ROTATE_CW if cw else _ROTATE_CCW)
        for _ in range(rotations):
            _rotate_pentagon_ccw(digits)
    else:
        for _ in range(rotations):
            _rotate_digits(digits, _ROTATE_CCW)

    index = (1 << 59) | (RESOLUTION << 52) | (base_cell << 45)
    for r in range(1, 16):
        index |= (digits[r] if r <= RESOLUTION else 7) << ((15 - r) * 3)
    return f"{index:x}"
