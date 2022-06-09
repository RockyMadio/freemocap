import logging

import cv2
import numpy as np

from src.core_processor.timestamp_manager.timestamp_manager import TimestampManager
from src.core_processor.utils.image_fps_writer import write_fps_to_image

logger = logging.getLogger(__name__)


def show_cam_window(webcam_id: str, image: np.array, timestamp_manager_cls: TimestampManager):
    cv2.imshow(webcam_id, image)
    return True
