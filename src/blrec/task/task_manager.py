from __future__ import annotations
import asyncio
import logging
from typing import Dict, Iterator, Optional, TYPE_CHECKING

import aiohttp
from tenacity import (
    retry,
    wait_exponential,
    stop_after_delay,
    retry_if_exception_type,
)

from .task import RecordTask
from .models import TaskData, TaskParam, VideoFileDetail, DanmakuFileDetail
from ..flv.data_analyser import MetaData
from ..core.stream_analyzer import StreamProfile
from ..exception import submit_exception, NotFoundError
from ..bili.exceptions import ApiRequestError
if TYPE_CHECKING:
    from ..setting import SettingsManager
from ..setting import (
    HeaderSettings,
    DanmakuSettings,
    RecorderSettings,
    PostprocessingSettings,
    TaskSettings,
    OutputSettings,
)


__all__ = 'RecordTaskManager',


logger = logging.getLogger(__name__)


class RecordTaskManager:
    def __init__(self, settings_manager: SettingsManager) -> None:
        self._settings_manager = settings_manager
        self._tasks: Dict[int, RecordTask] = {}

    async def load_all_tasks(self) -> None:
        logger.info('Loading all tasks...')

        settings_list = self._settings_manager.get_settings({'tasks'}).tasks
        assert settings_list is not None

        for settings in settings_list:
            try:
                await self.add_task(settings)
            except Exception as e:
                submit_exception(e)

        logger.info('Load all tasks complete')

    async def destroy_all_tasks(self) -> None:
        logger.info('Destroying all tasks...')
        if not self._tasks:
            return
        await asyncio.wait([
            t.destroy() for t in self._tasks.values() if t.ready
        ])
        self._tasks.clear()
        logger.info('Successfully destroyed all task')

    def has_task(self, room_id: int) -> bool:
        return room_id in self._tasks

    @retry(
        reraise=True,
        retry=retry_if_exception_type((
            asyncio.TimeoutError, aiohttp.ClientError, ApiRequestError,
        )),
        wait=wait_exponential(max=10),
        stop=stop_after_delay(60),
    )
    async def add_task(self, settings: TaskSettings) -> None:
        logger.info(f'Adding task {settings.room_id}...')

        task = RecordTask(settings.room_id)
        self._tasks[settings.room_id] = task

        try:
            await self._settings_manager.apply_task_header_settings(
                settings.room_id, settings.header, update_session=False
            )
            await task.setup()

            self._settings_manager.apply_task_output_settings(
                settings.room_id, settings.output
            )
            self._settings_manager.apply_task_danmaku_settings(
                settings.room_id, settings.danmaku
            )
            self._settings_manager.apply_task_recorder_settings(
                settings.room_id, settings.recorder
            )
            self._settings_manager.apply_task_postprocessing_settings(
                settings.room_id, settings.postprocessing
            )

            if settings.enable_monitor:
                await task.enable_monitor()
            if settings.enable_recorder:
                await task.enable_recorder()
        except Exception as e:
            logger.error(
                f'Failed to add task {settings.room_id} due to: {repr(e)}'
            )
            del self._tasks[settings.room_id]
            raise

        logger.info(f'Successfully added task {settings.room_id}')

    async def remove_task(self, room_id: int) -> None:
        task = self._get_task(room_id, check_ready=True)
        await task.disable_recorder(force=True)
        await task.disable_monitor()
        await task.destroy()
        del self._tasks[room_id]

    async def remove_all_tasks(self) -> None:
        coros = [
            self.remove_task(i) for i, t in self._tasks.items() if t.ready
        ]
        if coros:
            await asyncio.wait(coros)

    async def start_task(self, room_id: int) -> None:
        task = self._get_task(room_id, check_ready=True)
        await task.update_info()
        await task.enable_monitor()
        await task.enable_recorder()

    async def stop_task(self, room_id: int, force: bool = False) -> None:
        task = self._get_task(room_id, check_ready=True)
        await task.disable_recorder(force)
        await task.disable_monitor()

    async def start_all_tasks(self) -> None:
        await self.update_all_task_infos()
        await self.enable_all_task_monitors()
        await self.enable_all_task_recorders()

    async def stop_all_tasks(self, force: bool = False) -> None:
        await self.disable_all_task_recorders(force)
        await self.disable_all_task_monitors()

    async def enable_task_monitor(self, room_id: int) -> None:
        task = self._get_task(room_id, check_ready=True)
        await task.enable_monitor()

    async def disable_task_monitor(self, room_id: int) -> None:
        task = self._get_task(room_id, check_ready=True)
        await task.disable_monitor()

    async def enable_all_task_monitors(self) -> None:
        coros = [t.enable_monitor() for t in self._tasks.values() if t.ready]
        if coros:
            await asyncio.wait(coros)

    async def disable_all_task_monitors(self) -> None:
        coros = [t.disable_monitor() for t in self._tasks.values() if t.ready]
        if coros:
            await asyncio.wait(coros)

    async def enable_task_recorder(self, room_id: int) -> None:
        task = self._get_task(room_id, check_ready=True)
        await task.enable_recorder()

    async def disable_task_recorder(
            self, room_id: int, force: bool = False
    ) -> None:
        task = self._get_task(room_id, check_ready=True)
        await task.disable_recorder(force)

    async def enable_all_task_recorders(self) -> None:
        coros = [t.enable_recorder() for t in self._tasks.values() if t.ready]
        if coros:
            await asyncio.wait(coros)

    async def disable_all_task_recorders(self, force: bool = False) -> None:
        coros = [
            t.disable_recorder(force) for t in self._tasks.values() if t.ready
        ]
        if coros:
            await asyncio.wait(coros)

    def get_task_data(self, room_id: int) -> TaskData:
        task = self._get_task(room_id, check_ready=True)
        return self._make_task_data(task)

    def get_all_task_data(self) -> Iterator[TaskData]:
        for task in filter(lambda t: t.ready, self._tasks.values()):
            yield self._make_task_data(task)

    def get_task_param(self, room_id: int) -> TaskParam:
        task = self._get_task(room_id, check_ready=True)
        return self._make_task_param(task)

    def get_task_metadata(self, room_id: int) -> Optional[MetaData]:
        task = self._get_task(room_id, check_ready=True)
        return task.metadata

    def get_task_stream_profile(self, room_id: int) -> StreamProfile:
        task = self._get_task(room_id, check_ready=True)
        return task.stream_profile

    def get_task_video_file_details(
        self, room_id: int
    ) -> Iterator[VideoFileDetail]:
        task = self._get_task(room_id, check_ready=True)
        yield from task.video_file_details

    def get_task_danmaku_file_details(
        self, room_id: int
    ) -> Iterator[DanmakuFileDetail]:
        task = self._get_task(room_id, check_ready=True)
        yield from task.danmaku_file_details

    def can_cut_stream(self, room_id: int) -> bool:
        task = self._get_task(room_id, check_ready=True)
        return task.can_cut_stream()

    def cut_stream(self, room_id: int) -> bool:
        task = self._get_task(room_id, check_ready=True)
        return task.cut_stream()

    async def update_task_info(self, room_id: int) -> None:
        task = self._get_task(room_id, check_ready=True)
        await task.update_info()

    async def update_all_task_infos(self) -> None:
        coros = [t.update_info() for t in self._tasks.values() if t.ready]
        if coros:
            await asyncio.wait(coros)

    async def apply_task_header_settings(
        self,
        room_id: int,
        settings: HeaderSettings,
        *,
        update_session: bool = True,
    ) -> None:
        task = self._get_task(room_id)

        # avoid unnecessary updates that will interrupt connections
        if (
            task.user_agent == settings.user_agent and
            task.cookie == settings.cookie
        ):
            return

        task.user_agent = settings.user_agent
        task.cookie = settings.cookie

        if update_session:
            # update task session to take the effect
            await task.update_session()

    def apply_task_output_settings(
        self, room_id: int, settings: OutputSettings
    ) -> None:
        task = self._get_task(room_id)
        task.out_dir = settings.out_dir
        task.path_template = settings.path_template
        task.filesize_limit = settings.filesize_limit
        task.duration_limit = settings.duration_limit

    def apply_task_danmaku_settings(
        self, room_id: int, settings: DanmakuSettings
    ) -> None:
        task = self._get_task(room_id)
        task.danmu_uname = settings.danmu_uname
        task.record_gift_send = settings.record_gift_send
        task.record_free_gifts = settings.record_free_gifts
        task.record_guard_buy = settings.record_guard_buy
        task.record_super_chat = settings.record_super_chat
        task.save_raw_danmaku = settings.save_raw_danmaku

    def apply_task_recorder_settings(
        self, room_id: int, settings: RecorderSettings
    ) -> None:
        task = self._get_task(room_id)
        task.stream_format = settings.stream_format
        task.quality_number = settings.quality_number
        task.fmp4_stream_timeout = settings.fmp4_stream_timeout
        task.read_timeout = settings.read_timeout
        task.disconnection_timeout = settings.disconnection_timeout
        task.buffer_size = settings.buffer_size
        task.save_cover = settings.save_cover
        task.cover_save_strategy = settings.cover_save_strategy

    def apply_task_postprocessing_settings(
        self, room_id: int, settings: PostprocessingSettings
    ) -> None:
        task = self._get_task(room_id)
        task.remux_to_mp4 = settings.remux_to_mp4
        task.inject_extra_metadata = settings.inject_extra_metadata
        task.delete_source = settings.delete_source

    def _get_task(self, room_id: int, check_ready: bool = False) -> RecordTask:
        try:
            task = self._tasks[room_id]
        except KeyError:
            raise NotFoundError(f'no task for the room {room_id}')
        else:
            if check_ready and not task.ready:
                raise NotFoundError(f'the task {room_id} is not ready yet')
            return task

    def _make_task_param(self, task: RecordTask) -> TaskParam:
        return TaskParam(
            out_dir=task.out_dir,
            path_template=task.path_template,
            filesize_limit=task.filesize_limit,
            duration_limit=task.duration_limit,
            user_agent=task.user_agent,
            cookie=task.cookie,
            danmu_uname=task.danmu_uname,
            record_gift_send=task.record_gift_send,
            record_free_gifts=task.record_free_gifts,
            record_guard_buy=task.record_guard_buy,
            record_super_chat=task.record_super_chat,
            save_cover=task.save_cover,
            cover_save_strategy=task.cover_save_strategy,
            save_raw_danmaku=task.save_raw_danmaku,
            stream_format=task.stream_format,
            quality_number=task.quality_number,
            fmp4_stream_timeout=task.fmp4_stream_timeout,
            read_timeout=task.read_timeout,
            disconnection_timeout=task.disconnection_timeout,
            buffer_size=task.buffer_size,
            remux_to_mp4=task.remux_to_mp4,
            inject_extra_metadata=task.inject_extra_metadata,
            delete_source=task.delete_source,
        )

    def _make_task_data(self, task: RecordTask) -> TaskData:
        return TaskData(
            user_info=task.user_info,
            room_info=task.room_info,
            task_status=task.status,
        )
