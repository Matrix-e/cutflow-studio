import unittest
from datetime import datetime, timezone

from studio.progress import format_elapsed, progress_snapshot, STAGE_MESSAGES


def at(seconds):
    return datetime.fromtimestamp(1_800_000_000 + seconds, timezone.utc).isoformat()


def event(seconds, status, message="", **extra):
    return {"created_at": at(seconds), "status": status, "message": message, **extra}


def job(status="analyzing", mode="brand_promo", **extra):
    return {"status": status, "created_at": at(0), "updated_at": at(10),
            "settings": {"creation": {"mode": mode}}, **extra}


class ProgressTests(unittest.TestCase):
    def test_existing_real_messages_map_to_voice_stage_and_measured_elapsed(self):
        history = [event(0, "queued", "任务已加入队列"), event(5, "analyzing", "正在分析原始素材"),
                   event(10, "analyzing", "正在按已确认讲稿生成完整旁白")]
        value = progress_snapshot(job(production={"stage": "preparing_voice"}), history, current_time=at(40))
        self.assertEqual(value["stage"], "preparing_voice")
        self.assertEqual(value["completed_count"], 1)
        self.assertEqual(value["total_count"], 5)
        self.assertEqual(value["elapsed_seconds"], 40)
        self.assertEqual(value["stage_elapsed_seconds"], 30)
        self.assertNotIn("percent", value)
        self.assertIsNone(value["eta_seconds"])

    def test_person_mode_has_real_animation_phase_and_order(self):
        history = [event(0, "queued"), event(2, "analyzing"), event(7, "analyzing", stage="preparing_voice"),
                   event(30, "analyzing", "正在同步人物嘴型")]
        value = progress_snapshot(job(mode="talking_head", production={"stage": "animating_presenter"}), history, current_time=at(70))
        self.assertEqual(value["total_count"], 6)
        self.assertEqual(value["completed_count"], 2)
        self.assertEqual(value["stage_elapsed_seconds"], 40)
        self.assertEqual([entry["id"] for entry in value["stages"]], ["validating", "preparing_voice", "animating_presenter", "planning_visuals", "compositing", "checking"])

    def test_legacy_edit_only_has_analysis_render_check_and_no_imagined_voice(self):
        value = progress_snapshot(job(status="rendering", mode="edit"), [event(0, "queued"), event(1, "analyzing"),
                                  event(3, "rendering", "分析完成，开始渲染和成片检查")], current_time=at(13))
        self.assertEqual(value["stage"], "compositing")
        self.assertEqual(value["total_count"], 3)
        self.assertEqual([stage["label"] for stage in value["stages"]], ["分析素材", "渲染画面", "检查成片"])
        self.assertEqual(value["stage_elapsed_seconds"], 10)

    def test_restart_queue_resets_old_stage_old_plan_and_old_elapsed(self):
        history = [event(0, "queued"), event(5, "analyzing", stage="preparing_voice"), event(70, "rendering"),
                   event(100, "queued", "服务重新启动，已重新排队恢复剪辑")]
        value = progress_snapshot(job("queued", production={"stage": "compositing", "last_stage": "checking"}, plan={"old": True}),
                                  history, queue_position=3, current_time=at(106))
        self.assertEqual(value["stage"], "queued")
        self.assertEqual(value["completed_count"], 0)
        self.assertEqual(value["elapsed_seconds"], 6)
        self.assertEqual(value["stage_elapsed_seconds"], 6)
        self.assertIsNone(value["last_stage"])
        self.assertTrue(all(stage["state"] == "pending" for stage in value["stages"]))
        self.assertIn("等待队列中排第 3 位", value["detail"])

    def test_failure_retains_last_actual_phase_and_stops_the_clock(self):
        history = [event(0, "queued"), event(2, "analyzing", stage="preparing_voice"),
                   event(15, "analyzing", STAGE_MESSAGES["planning_visuals"]), event(18, "failed", "剪辑失败")]
        value = progress_snapshot(job("failed", production={"stage": "failed"}, error="画面短于旁白"), history, current_time=at(1000))
        self.assertEqual(value["stage"], "failed")
        self.assertEqual(value["last_stage"], "planning_visuals")
        self.assertEqual(value["completed_count"], 2)
        self.assertEqual(value["elapsed_seconds"], 18)
        self.assertEqual(value["stage_elapsed_seconds"], 3)
        self.assertEqual(value["stages"][2]["state"], "failed")
        self.assertIn("画面短于旁白", value["detail"])

    def test_completed_clocks_stop_at_review_not_later_approval_or_publication(self):
        history = [event(0, "queued"), event(10, "rendering"), event(18, "rendering", stage="checking"),
                   event(25, "review"), event(100, "approved"), event(110, "published")]
        value = progress_snapshot(job("published", updated_at=at(110)), history, current_time=at(1000))
        self.assertEqual(value["stage"], "ready")
        self.assertEqual(value["elapsed_seconds"], 25)
        self.assertEqual(value["completed_count"], value["total_count"])
        self.assertTrue(all(entry["state"] == "completed" for entry in value["stages"]))

    def test_publishing_failure_does_not_claim_the_render_failed(self):
        history = [event(0, "queued"), event(25, "review"), event(30, "publishing"), event(40, "failed")]
        value = progress_snapshot(job("failed", error="发送结果未知"), history, current_time=at(1000))
        self.assertEqual(value["last_stage"], "ready")
        self.assertEqual(value["completed_count"], value["total_count"])
        self.assertIn("成片已完成", value["detail"])
        self.assertEqual(value["elapsed_seconds"], 25)

    def test_explicit_stage_timestamp_and_repeated_phase_events_do_not_reset_elapsed(self):
        history = [event(0, "queued"), event(10, "analyzing", stage="preparing_voice"),
                   event(15, "analyzing", stage="preparing_voice")]
        value = progress_snapshot(job(production={"stage": "preparing_voice"}), history, current_time=at(20))
        self.assertEqual(value["stage_elapsed_seconds"], 10)
        value = progress_snapshot(job(production={"stage": "preparing_voice", "stage_started_at": at(12)}), history, current_time=at(20))
        self.assertEqual(value["stage_elapsed_seconds"], 8)

    def test_incomplete_timestamps_negative_clock_and_invalid_queue_are_safe(self):
        value = progress_snapshot({"status": "queued"}, [], queue_position=True, current_time=at(0))
        self.assertIsNone(value["elapsed_seconds"])
        self.assertIsNone(value["queue_position"])
        self.assertEqual(value["elapsed_label"], "暂无计时")
        value = progress_snapshot(job("queued"), [event(10, "queued")], queue_position=-1, current_time=at(0))
        self.assertEqual(value["elapsed_seconds"], 0)
        self.assertIsNone(value["queue_position"])

    def test_elapsed_format_is_not_a_countdown(self):
        for seconds, expected in [(0,"0秒"),(59.9,"59秒"),(60,"1分0秒"),(3661,"1小时1分1秒"),(-1,"0秒"),(None,"暂无计时")]:
            with self.subTest(seconds=seconds):
                self.assertEqual(format_elapsed(seconds), expected)


if __name__ == "__main__":
    unittest.main()
