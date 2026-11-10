from fnmusic.preferences import style_names
from fnmusic.ranking import metric, quality_rank


def test_style_aliases_and_real_rating_ranking():
    assert style_names("Rock;Jazz;流行;Pop;Unknown") == ["摇滚", "爵士", "流行"]
    assert metric("2.5万") == 25000
    assert metric("1亿") == 100000000
    assert metric("NaN") is None
    assert metric(True) is None
    assert quality_rank({"rating": 9}) > quality_rank({"rating": 2, "play_count": "10亿"})
    assert quality_rank({"likes": 20}) > quality_rank({"likes": 2})
    assert quality_rank({"play_count": "2万"}) > quality_rank({"play_count": "1万"})
    assert quality_rank({"comment_count": 10000}) == quality_rank({})
