import json
import os
import threading
import time
from abc import abstractmethod
from collections import defaultdict
from contextlib import contextmanager
from typing import IO, Iterator, Optional, Sequence, Union

from typing_extensions import TypeAlias

from dagster import _check as check
from dagster._core.instance import T_DagsterInstance
from dagster._core.storage.captured_log_manager import (
    CapturedLogContext,
    CapturedLogData,
    CapturedLogManager,
    CapturedLogMetadata,
    CapturedLogSubscription,
)
from dagster._core.storage.compute_log_manager import (
    MAX_BYTES_FILE_READ,
    ComputeIOType,
    ComputeLogFileData,
    ComputeLogManager,
    ComputeLogSubscription,
)
from dagster._core.storage.local_compute_log_manager import (
    IO_TYPE_EXTENSION,
    LocalComputeLogManager,
)

SUBSCRIPTION_POLLING_INTERVAL = 5

LogSubscription: TypeAlias = Union[CapturedLogSubscription, ComputeLogSubscription]


class CloudStorageComputeLogManager(CapturedLogManager, ComputeLogManager[T_DagsterInstance]):
    """Abstract class that uses the local compute log manager to capture logs and stores them in
    remote cloud storage.
    """

    @property
    @abstractmethod
    def local_manager(self) -> LocalComputeLogManager:
        """
        Returns a LocalComputeLogManager.
        """

    @property
    @abstractmethod
    def upload_interval(self) -> Optional[int]:
        """
        Returns the interval in which partial compute logs are uploaded to cloud storage.
        """

    @abstractmethod
    def delete_logs(
        self, log_key: Optional[Sequence[str]] = None, prefix: Optional[Sequence[str]] = None
    ) -> None:
        """
        Deletes logs for a given log_key or prefix.
        """

    @abstractmethod
    def download_url_for_type(self, log_key: Sequence[str], io_type: ComputeIOType) -> str:
        """
        Calculates a download url given a log key and compute io type.
        """

    @abstractmethod
    def display_path_for_type(self, log_key: Sequence[str], io_type: ComputeIOType) -> str:
        """
        Returns a display path given a log key and compute io type.
        """

    @abstractmethod
    def cloud_storage_has_logs(
        self, log_key: Sequence[str], io_type: ComputeIOType, partial: bool = False
    ) -> bool:
        """
        Returns whether the cloud storage contains logs for a given log key.
        """

    @abstractmethod
    def upload_to_cloud_storage(
        self, log_key: Sequence[str], io_type: ComputeIOType, partial: bool = False
    ) -> None:
        """
        Uploads the logs for a given log key from local storage to cloud storage.
        """

    def download_from_cloud_storage(
        self, log_key: Sequence[str], io_type: ComputeIOType, partial: bool = False
    ) -> None:
        """
        Downloads the logs for a given log key from cloud storage to local storage.
        """

    @contextmanager
    def capture_logs(self, log_key: Sequence[str]) -> Iterator[CapturedLogContext]:
        with self._poll_for_local_upload(log_key):
            with self.local_manager.capture_logs(log_key) as context:
                yield context
        self._on_capture_complete(log_key)

    @contextmanager
    def open_log_stream(
        self, log_key: Sequence[str], io_type: ComputeIOType
    ) -> Iterator[Optional[IO]]:
        with self.local_manager.open_log_stream(log_key, io_type) as f:
            yield f
        self._on_capture_complete(log_key)

    def _on_capture_complete(self, log_key: Sequence[str]):
        self.upload_to_cloud_storage(log_key, ComputeIOType.STDOUT)
        self.upload_to_cloud_storage(log_key, ComputeIOType.STDERR)

    def is_capture_complete(self, log_key: Sequence[str]) -> bool:
        if self.local_manager.is_capture_complete(log_key):
            return True
        # check remote storage
        return self.cloud_storage_has_logs(log_key, ComputeIOType.STDERR)

    def log_data_for_type(
        self, log_key: Sequence[str], io_type: ComputeIOType, offset: int, max_bytes: Optional[int]
    ):
        if self.has_local_file(log_key, io_type):
            local_path = self.local_manager.get_captured_local_path(
                log_key, IO_TYPE_EXTENSION[io_type]
            )
            return self.local_manager.read_path(local_path, offset=offset, max_bytes=max_bytes)
        if self.cloud_storage_has_logs(log_key, io_type):
            self.download_from_cloud_storage(log_key, io_type)
            local_path = self.local_manager.get_captured_local_path(
                log_key, IO_TYPE_EXTENSION[io_type]
            )
            return self.local_manager.read_path(local_path, offset=offset, max_bytes=max_bytes)
        if self.cloud_storage_has_logs(log_key, io_type, partial=True):
            self.download_from_cloud_storage(log_key, io_type, partial=True)
            local_path = self.local_manager.get_captured_local_path(
                log_key, IO_TYPE_EXTENSION[io_type], partial=True
            )
            return self.local_manager.read_path(local_path, offset=offset, max_bytes=max_bytes)

        return None, offset

    def get_log_data(
        self,
        log_key: Sequence[str],
        cursor: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> CapturedLogData:
        stdout_offset, stderr_offset = self.local_manager.parse_cursor(cursor)
        stdout, new_stdout_offset = self.log_data_for_type(
            log_key, ComputeIOType.STDOUT, stdout_offset, max_bytes
        )
        stderr, new_stderr_offset = self.log_data_for_type(
            log_key, ComputeIOType.STDERR, stderr_offset, max_bytes
        )
        return CapturedLogData(
            log_key=log_key,
            stdout=stdout,
            stderr=stderr,
            cursor=self.local_manager.build_cursor(new_stdout_offset, new_stderr_offset),
        )

    def get_log_metadata(self, log_key: Sequence[str]) -> CapturedLogMetadata:
        return CapturedLogMetadata(
            stdout_location=self.display_path_for_type(log_key, ComputeIOType.STDOUT),
            stderr_location=self.display_path_for_type(log_key, ComputeIOType.STDERR),
            stdout_download_url=self.download_url_for_type(log_key, ComputeIOType.STDOUT),
            stderr_download_url=self.download_url_for_type(log_key, ComputeIOType.STDERR),
        )

    def on_progress(self, log_key):
        # should be called at some interval, to be used for streaming upload implementations
        if self.is_capture_complete(log_key):
            return

        self.upload_to_cloud_storage(log_key, ComputeIOType.STDOUT, partial=True)
        self.upload_to_cloud_storage(log_key, ComputeIOType.STDERR, partial=True)

    def subscribe(
        self, log_key: Sequence[str], cursor: Optional[str] = None
    ) -> CapturedLogSubscription:
        subscription = CapturedLogSubscription(self, log_key, cursor)
        self.on_subscribe(subscription)  # type: ignore
        return subscription

    def unsubscribe(self, subscription):
        self.on_unsubscribe(subscription)

    def has_local_file(self, log_key: Sequence[str], io_type: ComputeIOType):
        local_path = self.local_manager.get_captured_local_path(log_key, IO_TYPE_EXTENSION[io_type])
        return os.path.exists(local_path)

    def _should_download(self, log_key: Sequence[str], io_type: ComputeIOType):
        return not self.has_local_file(log_key, io_type) and self.cloud_storage_has_logs(
            log_key, io_type
        )

    @contextmanager
    def _poll_for_local_upload(self, log_key: Sequence[str]) -> Iterator[None]:
        if not self.upload_interval:
            yield
            return

        thread_exit = threading.Event()
        thread = threading.Thread(
            target=_upload_partial_logs,
            args=(self, log_key, thread_exit, self.upload_interval),
            name="upload-watch",
        )
        thread.daemon = True
        thread.start()
        yield
        thread_exit.set()

    ###############################################
    #
    # Methods for the ComputeLogManager interface
    #
    ###############################################
    @contextmanager
    def _watch_logs(self, pipeline_run, step_key=None):
        # proxy watching to the local compute log manager, interacting with the filesystem
        log_key = self.local_manager.build_log_key_for_run(
            pipeline_run.run_id, step_key or pipeline_run.pipeline_name
        )
        with self.local_manager.capture_logs(log_key):
            yield
        self.upload_to_cloud_storage(log_key, ComputeIOType.STDOUT)
        self.upload_to_cloud_storage(log_key, ComputeIOType.STDERR)

    def get_local_path(self, run_id, key, io_type):
        return self.local_manager.get_local_path(run_id, key, io_type)

    def on_watch_start(self, pipeline_run, step_key):
        self.local_manager.on_watch_start(pipeline_run, step_key)

    def on_watch_finish(self, pipeline_run, step_key):
        self.local_manager.on_watch_finish(pipeline_run, step_key)

    def is_watch_completed(self, run_id, key):
        return self.local_manager.is_watch_completed(run_id, key) or self.cloud_storage_has_logs(
            self.local_manager.build_log_key_for_run(run_id, key), ComputeIOType.STDERR
        )

    def download_url(self, run_id, key, io_type):
        if not self.is_watch_completed(run_id, key):
            return None

        log_key = self.local_manager.build_log_key_for_run(run_id, key)
        return self.download_url_for_type(log_key, io_type)

    def read_logs_file(self, run_id, key, io_type, cursor=0, max_bytes=MAX_BYTES_FILE_READ):
        log_key = self.local_manager.build_log_key_for_run(run_id, key)

        if self.has_local_file(log_key, io_type):
            data = self.local_manager.read_logs_file(run_id, key, io_type, cursor, max_bytes)
            return self._from_local_file_data(run_id, key, io_type, data)
        elif self.cloud_storage_has_logs(log_key, io_type):
            self.download_from_cloud_storage(log_key, io_type)
            data = self.local_manager.read_logs_file(run_id, key, io_type, cursor, max_bytes)
            return self._from_local_file_data(run_id, key, io_type, data)
        elif self.cloud_storage_has_logs(log_key, io_type, partial=True):
            self.download_from_cloud_storage(log_key, io_type, partial=True)
            partial_path = self.local_manager.get_captured_local_path(
                log_key, IO_TYPE_EXTENSION[io_type], partial=True
            )
            captured_data, new_cursor = self.local_manager.read_path(
                partial_path, offset=cursor or 0
            )
            return ComputeLogFileData(
                path=partial_path,
                data=captured_data.decode("utf-8") if captured_data else None,
                cursor=new_cursor or 0,
                size=len(captured_data) if captured_data else 0,
                download_url=None,
            )
        local_path = self.local_manager.get_captured_local_path(log_key, IO_TYPE_EXTENSION[io_type])
        return ComputeLogFileData(path=local_path, data=None, cursor=0, size=0, download_url=None)

    def on_subscribe(self, subscription):
        pass

    def on_unsubscribe(self, subscription):
        pass

    def dispose(self):
        self.local_manager.dispose()

    def _from_local_file_data(self, run_id, key, io_type, local_file_data):
        log_key = self.local_manager.build_log_key_for_run(run_id, key)

        return ComputeLogFileData(
            self.display_path_for_type(log_key, io_type),
            local_file_data.data,
            local_file_data.cursor,
            local_file_data.size,
            self.download_url_for_type(log_key, io_type),
        )


class PollingComputeLogSubscriptionManager:
    def __init__(self, manager):
        self._manager = manager
        self._subscriptions = defaultdict(list)
        self._shutdown_event = None
        self._polling_thread = None

    def _log_key(self, subscription: LogSubscription) -> Sequence[str]:
        check.inst_param(
            subscription, "subscription", (ComputeLogSubscription, CapturedLogSubscription)
        )

        if isinstance(subscription, ComputeLogSubscription):
            return self._manager.build_log_key_for_run(subscription.run_id, subscription.key)
        return subscription.log_key

    def _watch_key(self, log_key: Sequence[str]) -> str:
        return json.dumps(log_key)

    def _start_polling_thread(self) -> None:
        if self._polling_thread:
            return

        self._shutdown_event = threading.Event()
        self._polling_thread = threading.Thread(
            target=self._poll,
            args=[self._shutdown_event],
            name="polling-compute-log-subscription",
        )
        self._polling_thread.daemon = True
        self._polling_thread.start()

    def _stop_polling_thread(self) -> None:
        if not self._polling_thread:
            return

        old_shutdown_event = self._shutdown_event
        old_shutdown_event.set()  # set to signal to the old thread to die  # type: ignore
        self._polling_thread = None
        self._shutdown_event = None

    def add_subscription(self, subscription: LogSubscription) -> None:
        check.inst_param(
            subscription, "subscription", (ComputeLogSubscription, CapturedLogSubscription)
        )

        if not self._polling_thread:
            self._start_polling_thread()

        if self.is_complete(subscription):
            subscription.fetch()
            subscription.complete()
        else:
            log_key = self._log_key(subscription)
            watch_key = self._watch_key(log_key)
            self._subscriptions[watch_key].append(subscription)

    def is_complete(self, subscription: LogSubscription) -> bool:
        check.inst_param(
            subscription, "subscription", (ComputeLogSubscription, CapturedLogSubscription)
        )

        if isinstance(subscription, ComputeLogSubscription):
            return self._manager.is_watch_completed(subscription.run_id, subscription.key)
        return self._manager.is_capture_complete(subscription.log_key)

    def remove_subscription(self, subscription: LogSubscription) -> None:
        check.inst_param(
            subscription, "subscription", (ComputeLogSubscription, CapturedLogSubscription)
        )
        log_key = self._log_key(subscription)
        watch_key = self._watch_key(log_key)

        if subscription in self._subscriptions[watch_key]:
            self._subscriptions[watch_key].remove(subscription)
            if len(self._subscriptions[watch_key]) == 0:
                del self._subscriptions[watch_key]
            subscription.complete()

        if not len(self._subscriptions) and self._polling_thread:
            self._stop_polling_thread()

    def remove_all_subscriptions(self, log_key: Sequence[str]) -> None:
        watch_key = self._watch_key(log_key)
        for subscription in self._subscriptions.pop(watch_key, []):
            subscription.complete()

        if not len(self._subscriptions) and self._polling_thread:
            self._stop_polling_thread()

    def notify_subscriptions(self, log_key: Sequence[str]) -> None:
        watch_key = self._watch_key(log_key)
        for subscription in self._subscriptions[watch_key]:
            subscription.fetch()

    def _poll(self, shutdown_event: threading.Event) -> None:
        while True:
            if shutdown_event.is_set():
                return
            # need to do something smarter here that keeps track of updates
            for _, subscriptions in self._subscriptions.items():
                for subscription in subscriptions:
                    if shutdown_event.is_set():
                        return
                    subscription.fetch()
            time.sleep(SUBSCRIPTION_POLLING_INTERVAL)

    def dispose(self) -> None:
        if self._shutdown_event:
            self._shutdown_event.set()


def _upload_partial_logs(
    compute_log_manager: CloudStorageComputeLogManager,
    log_key: Sequence[str],
    thread_exit: threading.Event,
    interval: int,
) -> None:
    while True:
        time.sleep(interval)
        if thread_exit.is_set() or compute_log_manager.is_capture_complete(log_key):
            return
        compute_log_manager.on_progress(log_key)
