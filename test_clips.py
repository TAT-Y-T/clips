import json
import tempfile
import unittest
from pathlib import Path

import clips


class ClipOutputPlanTests(unittest.TestCase):
    def test_segments_annotation_plans_four_views_and_left_eye_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recording_dir = Path(tmp)
            for view in clips.VIEWS:
                (recording_dir / f"recording_{view}.mp4").touch()
            annotation_path = recording_dir / "annotation.json"
            annotation_path.write_text(
                json.dumps(
                    {
                        "annotationTimebase": "aligned",
                        "instances": {
                            "segments": [
                                {
                                    "id": "0eabb199-5d7f-46e8-a74a-a148af846bbd",
                                    "group": [{"startTime": 12.5, "endTime": 18.0}],
                                    "attributes": {"description": "擦拭容器"},
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            plan = clips.build_aligned_input_clip_plan(
                recording_dir,
                annotation_path,
                output_dir=recording_dir / "clip",
                include_only_left=True,
                simple_output_names=True,
            )

            section = plan["sections"][0]
            self.assertEqual(
                set(section["clips"]),
                {"primary", "secondary", "left_hand", "right_hand", "only_left"},
            )
            self.assertEqual(
                Path(section["clips"]["only_left"]["output_path"]).parent.name,
                "section_0eabb199-5d7f-46e8-a74a-a148af846bbd",
            )
            self.assertEqual(
                {Path(item["output_path"]).name for item in section["clips"].values()},
                {
                    "primary.mp4",
                    "secondary.mp4",
                    "left_hand.mp4",
                    "right_hand.mp4",
                    "only_left.mp4",
                },
            )
            clips.write_manifest(plan, recording_dir / "clip")
            description_path = (
                recording_dir
                / "clip"
                / "section_0eabb199-5d7f-46e8-a74a-a148af846bbd"
                / "describe.txt"
            )
            self.assertEqual(description_path.read_text(encoding="utf-8"), "擦拭容器\n")

    def test_left_eye_filter_preserves_1920_by_1200_crop_without_scaling(self) -> None:
        command = clips.build_left_only_ffmpeg_command(
            Path("primary.mp4"),
            Path("only_left.mp4"),
            12.5,
            18.0,
        )

        video_filter = command[command.index("-vf") + 1]
        self.assertIn("crop=(iw-160)/2:ih:160:0", video_filter)
        self.assertNotIn("scale=", video_filter)


class CliCompatibilityTests(unittest.TestCase):
    def test_reencode_and_both_annotation_timebases_remain_available(self) -> None:
        aligned = clips.parse_args(
            ["/tmp/recording", "--reencode", "--make-grid", "--annotation-timebase", "aligned"]
        )
        raw = clips.parse_args(["/tmp/recording", "--annotation-timebase", "raw-primary"])

        self.assertTrue(aligned.reencode)
        self.assertTrue(aligned.make_grid)
        self.assertEqual(aligned.annotation_timebase, "aligned")
        self.assertEqual(raw.annotation_timebase, "raw-primary")

        reencode_command = clips.build_ffmpeg_command(
            Path("primary.mp4"),
            Path("primary_clip.mp4"),
            12.5,
            18.0,
            reencode=True,
        )
        self.assertIn("libx264", reencode_command)
        self.assertNotIn("copy", reencode_command)


if __name__ == "__main__":
    unittest.main()
