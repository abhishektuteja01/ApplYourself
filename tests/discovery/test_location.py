from src.discovery.location import parse_location

def test_location_austin_tx():
    res = parse_location("Austin, TX")
    assert res.country == "United States"
    assert res.state == "TX"
    assert res.city == "Austin"
    assert res.remote is False

def test_location_remote():
    res = parse_location("Remote")
    assert res.remote is True
    assert res.country == ""
    assert res.state == ""
    assert res.city == ""

def test_location_empty():
    res = parse_location("")
    assert res.country == ""
    assert res.state == ""
    assert res.city == ""
    assert res.remote is False

def test_location_berlin_germany():
    res = parse_location("Berlin, Germany")
    assert res.country == "Germany"

def test_location_sf_bay_area():
    res = parse_location("San Francisco Bay Area")
    # city San Francisco OR all-empty
    if res.city == "San Francisco":
        assert res.country == "United States"
        assert res.state == "CA"
    else:
        assert res.country == ""
        assert res.state == ""
        assert res.city == ""

def test_location_multiple():
    res = parse_location("New York, London or Singapore")
    # must NOT resolve to a single US location (all-empty acceptable)
    if res.country != "":
        # if it resolves to something, it cannot be US-only. But spec says all-empty acceptable.
        assert res.country == ""
        assert res.state == ""
        assert res.city == ""

def test_location_bare_ca():
    res = parse_location("CA")
    assert res.country == ""
    assert res.state == ""
    assert res.city == ""

def test_location_remote_austin():
    res = parse_location("Remote - Austin, TX")
    assert res.remote is True
    assert res.country == "United States"
    assert res.state == "TX"
    assert res.city == "Austin"
