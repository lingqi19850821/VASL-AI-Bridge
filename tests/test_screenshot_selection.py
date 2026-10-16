"""Pixel selection and immutable evidence checks using synthetic screenshots."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from io import BytesIO
import unittest

from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from vasl_ai_bridge.capture import prepare_image
from vasl_ai_bridge.screenshot_selection import (
    ScreenshotSelectionDialog, crop_observation, image_box_from_drag,
)


APP = QApplication.instance() or QApplication(["Screenshot selection tests"])


class ScreenshotSelectionTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (800, 400), "white")
        self.image.paste("red", (100, 40, 300, 200))
        self.observation = prepare_image(self.image, "Synthetic VASL", 8192)

    def test_scaled_letterboxed_drag_reverse_and_edges(self):
        # An 800 x 400 screenshot fitted into a 400 x 400 canvas has 100 px top padding.
        args = ((800, 400), (0.0, 100.0, 400.0, 200.0))
        self.assertEqual(image_box_from_drag(*args, (50, 120), (150, 200)), (100, 40, 300, 200))
        self.assertEqual(image_box_from_drag(*args, (150, 200), (50, 120)), (100, 40, 300, 200))
        self.assertEqual(image_box_from_drag(*args, (-20, 90), (450, 350)), (0, 0, 800, 400))
        self.assertIsNone(image_box_from_drag(*args, (80, 120), (80, 200)))
        self.assertIsNone(image_box_from_drag(*args, (80, 80), (100, 90)))

    def test_crop_is_exact_pixels_independent_evidence_and_original_time(self):
        original_bytes = self.observation.png
        selected = crop_observation(self.observation, (100, 40, 300, 200))
        self.assertEqual(selected.captured_at, self.observation.captured_at)
        self.assertNotEqual(selected.observation_id, self.observation.observation_id)
        self.assertEqual((selected.width, selected.height), (200, 160))
        with Image.open(BytesIO(selected.png)) as decoded:
            self.assertEqual(decoded.tobytes(), self.image.crop((100, 40, 300, 200)).tobytes())
        self.assertEqual(self.observation.png, original_bytes)

    def test_resize_occurs_after_crop_and_full_image_choice(self):
        large = Image.new("RGB", (4000, 2000), "white")
        large.paste("blue", (2500, 100, 3500, 1100))
        frozen = prepare_image(large, "Large synthetic", 8192)
        cropped = crop_observation(frozen, (2500, 100, 3500, 1100))
        self.assertEqual((cropped.width, cropped.height), (1000, 1000))
        with Image.open(BytesIO(cropped.png)) as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (0, 0, 255))
        dialog = ScreenshotSelectionDialog(frozen)
        dialog.keep_full_image()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual((dialog.selected_observation.width, dialog.selected_observation.height), (2000, 1000))
        self.assertEqual(dialog.selected_observation.captured_at, frozen.captured_at)
        dialog.close()

    def test_invalid_crop_never_pads_unseen_pixels(self):
        for box in ((-1, 0, 20, 20), (0, 0, 801, 400), (20, 20, 10, 30), (0, 0, 0, 0),
                    (False, 0, 20, 20), (0.5, 0, 20, 20)):
            with self.subTest(box=box), self.assertRaises(ValueError):
                crop_observation(self.observation, box)

    def test_real_canvas_drag_and_cancel_do_not_replace_frozen_image(self):
        dialog = ScreenshotSelectionDialog(self.observation)
        dialog.resize(500, 450)
        dialog.show()
        APP.processEvents()
        canvas = dialog.canvas
        rect = canvas.image_rect()
        start = QPoint(round(rect.x() + rect.width() * .25), round(rect.y() + rect.height() * .25))
        end = QPoint(round(rect.x() + rect.width() * .75), round(rect.y() + rect.height() * .75))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
        self.assertTrue(dialog.use_button.isEnabled())
        box = canvas.selection_box
        self.assertAlmostEqual(box[0], 200, delta=3)
        self.assertAlmostEqual(box[1], 100, delta=3)
        self.assertAlmostEqual(box[2], 600, delta=3)
        self.assertAlmostEqual(box[3], 300, delta=3)
        canvas.resize(600, 300)
        self.assertEqual(canvas.selection_box, box)
        dialog.reject()
        self.assertIsNone(dialog.selected_observation)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertIs(dialog.observation, self.observation)
        dialog.close()

    def test_use_selection_accepts_only_when_an_area_exists(self):
        dialog = ScreenshotSelectionDialog(self.observation)
        dialog.use_selection()
        self.assertIsNone(dialog.selected_observation)
        dialog.canvas.selection_box = (100, 40, 300, 200)
        dialog.use_selection()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.selected_observation.captured_at, self.observation.captured_at)
        self.assertEqual((dialog.selected_observation.width, dialog.selected_observation.height), (200, 160))
        dialog.close()


if __name__ == "__main__":
    unittest.main()
