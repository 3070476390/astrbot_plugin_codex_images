import unittest

from codex_images.quota import format_quota, normalize_quota


class QuotaTests(unittest.TestCase):
    def test_multiple_buckets_take_precedence_and_legacy_fallback(self):
        data = normalize_quota({
            "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10}},
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 0}},
                "other": {"primary": {"usedPercent": 100, "windowDurationMins": 10080}},
            },
            "ordinaryUsageAllowed": False,
        })
        self.assertEqual(len(data["buckets"]), 2)
        self.assertEqual(data["buckets"][0]["windows"][0]["remaining_percent"], 75)
        self.assertEqual(data["buckets"][1]["windows"][0]["remaining_percent"], 0)
        text = format_quota(data, "统一账号")
        for expected in ["5 小时窗口", "7 天窗口", "1970-01-01 08:00:00", "UTC+8", "当前不允许", "不能换算"]:
            self.assertIn(expected, text)
        legacy = normalize_quota({"rateLimits": {"primary": {"usedPercent": 0}}})
        self.assertEqual(legacy["buckets"][0]["windows"][0]["remaining_percent"], 100)

    def test_unknown_is_not_zero_and_invalid_values_never_look_available(self):
        empty = format_quota(normalize_quota({"rateLimits": {}, "rateLimitsByLimitId": None}), "我的账号")
        self.assertIn("暂未提供", empty)
        self.assertNotIn("剩余 0%", empty)
        for used in [None, True, "25", -5, float("nan"), float("inf")]:
            with self.subTest(used=used):
                data = normalize_quota({"rateLimits": {"primary": {"usedPercent": used, "resetsAt": -1}}})
                window = data["buckets"][0]["windows"][0]
                self.assertIsNone(window["remaining_percent"])
                self.assertIsNone(window["resets_at"])
                self.assertIn("用量未提供", format_quota(data, "我的账号"))

    def test_credits_are_separate_from_windows_and_only_public_fields_survive(self):
        data = normalize_quota({
            "accountId": "secret-account", "token": "secret-token", "email": "secret-email",
            "rateLimits": {"primary": {"usedPercent": 120}, "credits": {"unlimited": True, "hasCredits": True},
                           "rateLimitReachedType": "workspace_member_credits_depleted", "spendControlReached": True},
            "rateLimitResetCredits": {"availableCount": 5, "credits": [{"id": "secret-id"}]},
        })
        self.assertNotIn("secret", str(data))
        text = format_quota(data, "统一账号")
        for expected in ["剩余 0%", "已用 120%", "Credits 余额：不限额", "重置次数：5", "成员额度已耗尽", "支出控制"]:
            self.assertIn(expected, text)
        data = normalize_quota({"rateLimits": {"credits": {"balance": "0", "hasCredits": False}}})
        self.assertIn("Credits 余额：0", format_quota(data, "我的账号"))

    def test_bounded_labels_buckets_and_chat_output(self):
        data = normalize_quota({"rateLimitsByLimitId": {str(i): {"limitName": "x" * 200} for i in range(25)}})
        self.assertEqual(len(data["buckets"]), 20)
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["buckets"][0]["name"]), 100)
        self.assertIn("前 6", format_quota(data, "我的账号"))
