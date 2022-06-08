import logging
import time
import traceback
from contextlib import contextmanager
from typing import ContextManager, Dict, List, Optional, Union

import numpy as np
from pydantic import BaseModel

from src.api.services.user_config import UserConfigService
from src.cameras.capture.opencv_camera.opencv_camera import OpenCVCamera
from src.cameras.detection.cam_singleton import get_or_create_cams
from src.cameras.persistence.video_writer.video_recorder import VideoRecorder
from src.config.webcam_config import WebcamConfig
from src.core_processor.fps.timestamp_manager import TimestampManager
from src.pipelines.session_pipeline.data_classes.multi_frame_payload import MultiFramePayload

logger = logging.getLogger(__name__)


class CamAndWriterResponse(BaseModel):
    cv_camera: OpenCVCamera
    video_recorder: Optional[VideoRecorder]

    class Config:
        arbitrary_types_allowed = True


class OpenCVCameraManager:
    def __init__(self,
                 session_id: str = None,
                 expected_framerate: Union[int, float] = None,
                 ):
        self._session_id = session_id
        self._config_service = UserConfigService()
        self._detected_cams_data = get_or_create_cams()
        self._session_start_time_unix_ns = None
        self._timestamp_manager: Union[TimestampManager, None] = None
        self._connected_cameras_dict = {}
        self._expected_framerate = expected_framerate

    @property
    def timestamp_manager(self):
        if self._timestamp_manager is None:
            logger.error('timestamp manager has not been created yet')
        else:
            return self._timestamp_manager

    @property
    def available_webcam_ids(self):
        return [cam_id.webcam_id for cam_id in get_or_create_cams().cameras_found_list]

    @property
    def latest_multi_frame(self) -> Union[MultiFramePayload,None]:
        if not self.new_multi_frame_ready():
            logging.error("a multi_frame was requested before it was ready!")
            raise Exception

        this_multi_frame_dict = {}
        for this_cam in self._connected_cameras_dict.values():
            if not this_cam.new_frame_ready:
                logger.error('It shouldnt be able to get here if the `new_multi_frame`')
            this_multi_frame_dict[this_cam.webcam_id_as_str] = this_cam.latest_frame
        self._timestamp_manager.log_new_multi_frame_timestamp_ns(time.perf_counter_ns())

        if not self._timestamp_manager.verify_multi_frame_is_synchronized(this_multi_frame_dict,
                                                                      self._expected_framerate):
            m_f_interval = self._timestamp_manager.latest_multi_frame_interval
            m_f_number = self._timestamp_manager.multi_frame_timestamp_logger.number_of_frames
            logger.error(f"Multi frame is not synchronized!! Skipping this one - multi_frame_frame_number|interval: {m_f_number}|{m_f_interval:.3f}")
            # raise Exception
            return None

        return MultiFramePayload(frames_dict=this_multi_frame_dict,
                                     multi_frame_number=self._timestamp_manager.multi_frame_timestamp_logger.number_of_frames,
                                     intra_frame_interval=self._timestamp_manager.latest_multi_frame_interval,
                                     each_frame_timestamp=self._timestamp_manager.latest_multi_frame_timestamp_list,
                                     multi_frame_timestamp=self._timestamp_manager.multi_frame_timestamp_logger.latest_timestamp,
                                     )

    def new_multi_frame_ready(self):
        """cycle through connected cameras and return false if one isn't read yet
        TODO - make another kind of check that uses a clock and sends an empty frame if one camera hasn't yeild a frame within a reasonable time period. With this method, the whol camera system will stall if one camera stops producing frames"""
        for this_cam in self._connected_cameras_dict.values():
            if not this_cam.new_frame_ready:
                return False
        return True

    @contextmanager
    def start_capture_session_single_cam(
            self, webcam_id: str
    ) -> ContextManager[CamAndWriterResponse]:
        """
        Context manager for easy start up, usage, and cleanup of camera resources.
        Can capture frames from a single webcam or all webcams detected.
        """

        cv_camera = self._create_single_opencv_cam(webcam_id)
        self._connected_cameras_dict['0'] = cv_camera
        try:
            self._start_frame_capture_on_cam_id(cv_camera)
            self._initialize_timestamp_logger()  # start timestamp logger and wha
            yield CamAndWriterResponse(cv_camera=cv_camera)
            self._stop_frame_capture([cv_camera])
        except:
            logger.error("Printing traceback from starting capture session by cam")
            traceback.print_exc()

    @contextmanager
    def start_capture_session_all_cams(
            self,
            calibration_videos=False,
    ) -> ContextManager[Dict[str, OpenCVCamera]]:
        open_cv_camera_objects = self._create_opencv_cameras(calibration_videos=calibration_videos)
        try:

            for cv_cam in open_cv_camera_objects:
                self._connected_cameras_dict[cv_cam.webcam_id_as_str] = cv_cam
                self._start_frame_capture_on_cam_id(cv_cam)
            self._initialize_timestamp_logger()
            yield self._connected_cameras_dict

            self._stop_frame_capture(open_cv_camera_objects)
        except:
            logger.error("Printing traceback from starting capture session by cam")
            traceback.print_exc()

    def _create_opencv_cameras(self, calibration_videos: bool = False):
        raw_camera_objects = self._detected_cams_data.cameras_found_list
        open_cv_cameras: List[OpenCVCamera] = []
        for this_raw_cam in raw_camera_objects:
            opencv_cam_obj = self._create_single_opencv_cam(this_raw_cam.webcam_id,
                                                            calibration_video_bool=calibration_videos)
            open_cv_cameras.append(opencv_cam_obj)
        return open_cv_cameras

    def _create_single_opencv_cam(self, webcam_id: str, calibration_video_bool: bool = False):
        webcam_config_model = self._config_service.webcam_config_by_id(webcam_id, self._session_id)
        single_camera_config = WebcamConfig(webcam_id=webcam_config_model.webcam_id,
                                            exposure=webcam_config_model.exposure,
                                            resolution_width=webcam_config_model.resolution_width,
                                            resolution_height=webcam_config_model.resolution_height, )
        return OpenCVCamera(config=single_camera_config,
                            session_id=self._session_id,
                            calibration_video_bool=calibration_video_bool)

    def _initialize_timestamp_logger(self):
        self._session_start_time_unix_ns = time.time_ns()
        self._session_start_time_perf_counter_ns = time.perf_counter_ns()
        self._timestamp_manager = TimestampManager(self.available_webcam_ids,
                                                   self._session_start_time_unix_ns,
                                                   self._session_start_time_perf_counter_ns)

    def _start_frame_capture_on_cam_id(self, opencv_cam: OpenCVCamera):
        opencv_cam.connect()
        opencv_cam.start_frame_capture_thread()

    def _stop_frame_capture(self, opencv_cam_objs: List[OpenCVCamera]):
        for cv_cam in opencv_cam_objs:
            cv_cam.stop_frame_capture()
