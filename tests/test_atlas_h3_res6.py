"""Pure-Python H3 res-6 cells equal the official `h3` 4.5.0 `latlng_to_cell` (T-500).

Vectors were generated once with `h3.latlng_to_cell(lat, lng, 6)`: Polish cities, poles,
the antimeridian, every res-0 pentagon centre, points next to them and random points.
"""

import pytest

from custom_components.energy_compass.atlas.h3_res6 import h3_res6_index

# fmt: off
VECTORS = (
    (52.2297, 21.0122, "861f53c97ffffff"),
    (50.0647, 19.945, "861e2e6b7ffffff"),
    (54.352, 18.6466, "861f09b27ffffff"),
    (51.1079, 17.0385, "861e2040fffffff"),
    (53.1325, 23.1688, "861f51337ffffff"),
    (49.2992, 19.9496, "861e01947ffffff"),
    (50.2649, 19.0238, "861e232c7ffffff"),
    (51.7592, 19.456, "861e21b17ffffff"),
    (52.4064, 16.9252, "861e24aa7ffffff"),
    (53.4285, 14.5528, "861f0e797ffffff"),
    (53.0138, 18.5984, "861f5652fffffff"),
    (50.6751, 17.9213, "861e23c67ffffff"),
    (51.2465, 22.5684, "861e2d08fffffff"),
    (50.0412, 21.9991, "861e286c7ffffff"),
    (53.7784, 20.4801, "861f5439fffffff"),
    (50.8661, 20.6286, "861e2eb5fffffff"),
    (0.0, 0.0, "86754e64fffffff"),
    (90.0, 0.0, "860326237ffffff"),
    (-90.0, 0.0, "86f29380fffffff"),
    (0.0, 180.0, "867eb5727ffffff"),
    (-33.8688, 151.2093, "86be0e35fffffff"),
    (64.1466, -21.9426, "86075dd4fffffff"),
    (-54.8019, -68.303, "86df45177ffffff"),
    (35.6762, 139.6503, "862f5a367ffffff"),
    (-77.85, 166.67, "86ed4984fffffff"),
    (64.7, 10.5362, "860800007ffffff"),
    (50.1032, -143.4785, "861c00007ffffff"),
    (39.1, 122.3, "863000007ffffff"),
    (23.7179, -67.1323, "864c00007ffffff"),
    (10.4473, 58.1577, "866200007ffffff"),
    (2.3009, -5.2454, "867400007ffffff"),
    (-2.3009, 174.7546, "867e00007ffffff"),
    (-10.4473, -121.8423, "869000007ffffff"),
    (-23.7179, 112.8677, "86a600007ffffff"),
    (-39.1, -57.7, "86c200007ffffff"),
    (-50.1032, 36.5215, "86d600007ffffff"),
    (-64.7, -169.4638, "86ea00007ffffff"),
    (65.4, 11.4362, "8608052d7ffffff"),
    (50.8032, -142.5785, "861c04b87ffffff"),
    (39.8, 123.2, "8630058afffffff"),
    (24.4179, -66.2323, "864c04957ffffff"),
    (11.1473, 59.0577, "866205957ffffff"),
    (3.0009, -4.3454, "86742346fffffff"),
    (-1.6009, 175.6546, "867e0259fffffff"),
    (-9.7473, -120.9423, "8690026c7ffffff"),
    (-23.0179, 113.7677, "86a61532fffffff"),
    (-38.4, -56.8, "86c21c90fffffff"),
    (-49.4032, 37.4215, "86d60254fffffff"),
    (-64.0, -168.5638, "86ea0694fffffff"),
    (-31.3578, -124.996, "86b12900fffffff"),
    (26.8663, -153.0678, "86368ad57ffffff"),
    (6.387, -48.0834, "865fb1217ffffff"),
    (-78.6762, 2.662, "86ef86a9fffffff"),
    (-82.3258, -23.7548, "86ef102d7ffffff"),
    (-76.5657, -146.5247, "86f22112fffffff"),
    (-13.4356, 117.0131, "869582627ffffff"),
    (-66.9633, -99.0805, "86e91b8b7ffffff"),
    (22.6831, 160.2798, "864f9c19fffffff"),
    (13.7243, -36.9884, "8656116b7ffffff"),
    (84.7734, -162.3234, "860316c2fffffff"),
    (63.8074, -75.3199, "860e3594fffffff"),
    (-63.3226, -136.8304, "86e235d37ffffff"),
    (-34.0902, 113.1732, "86c9994a7ffffff"),
    (-56.8307, 29.2129, "86e694927ffffff"),
    (24.7266, -45.6817, "863a53417ffffff"),
    (8.4985, -156.5215, "865c8519fffffff"),
    (-78.391, -105.2668, "86f2ee7afffffff"),
    (32.1112, -25.922, "86341d147ffffff"),
    (-33.0818, 30.6311, "86bc0e117ffffff"),
)
# fmt: on


@pytest.mark.parametrize("latitude,longitude,expected", VECTORS)
def test_cell_matches_official_h3(latitude, longitude, expected):
    assert h3_res6_index(latitude, longitude) == expected
