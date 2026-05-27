from services.yunlin_boundary import is_in_yunlin_county, yunlin_polygons


def test_yunlin_boundary_keeps_mainland_points():
    assert is_in_yunlin_county(23.69602, 120.533793)


def test_yunlin_boundary_excludes_offshore_fragments():
    # 外傘頂洲 is administratively Yunlin, but excluded from bus service area.
    assert not is_in_yunlin_county(23.485625847468274, 120.0476144030288)


def test_yunlin_boundary_loads_mainland_polygon_only():
    assert len(yunlin_polygons()) == 1
