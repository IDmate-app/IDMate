"""A field-type conflict in the bucket must not drop the whole point: the
bucket's type is learned from the error and the point written again."""

import config

MSG = ('(422) Reason: Unprocessable Entity HTTP response body: {"code":"unprocessable entity",'
       '"message":"failure writing points to database: partial write: field type conflict: '
       'input field \\"v\\" on measurement \\"v\\" is type float, already exists as type integer dropped=1"}')


def test_learns_the_bucket_type_from_the_error(monkeypatch):
    monkeypatch.setattr(config, "INFLUX_TYPE_LEARNED", {})
    assert config.influx_field_value("v", 101.4) == 101.4             # default: float
    assert config.learn_influx_type_conflict(MSG) == [("v", "integer")]
    assert config.influx_field_value("v", 101.4) == 101                # bucket wins
    assert config.learn_influx_type_conflict(MSG) == []                # nothing new
    assert config.learn_influx_type_conflict("timeout") == []


class _Writer:
    def __init__(self, fail_times):
        self.fail, self.points = fail_times, []

    def write(self, bucket, record):
        if self.fail:
            self.fail -= 1
            raise Exception(MSG)
        self.points.append(record.to_line_protocol())


def test_point_written_again_with_the_learned_type(app_module, monkeypatch):
    monkeypatch.setattr(config, "INFLUX_TYPE_LEARNED", {})
    w = _Writer(fail_times=1)
    err = app_module.influx_write_v(w, "dev1", {"s": 62.3, "v": 101.4, "u": 417}, 1_793_000_000)
    assert err is None and len(w.points) == 1
    line = w.points[0]
    assert "v=101i" in line and "s=62.3" in line and "u=417i" in line


def test_other_errors_are_returned_not_retried(app_module, monkeypatch):
    monkeypatch.setattr(config, "INFLUX_TYPE_LEARNED", {})

    class Down:
        n = 0

        def write(self, bucket, record):
            Down.n += 1
            raise Exception("connection refused")
    err = app_module.influx_write_v(Down(), "dev1", {"s": 62.3}, None)
    assert "refused" in str(err) and Down.n == 1
