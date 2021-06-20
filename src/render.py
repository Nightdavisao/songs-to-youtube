# This Python file uses the following encoding: utf-8

from PySide6.QtCore import QThread, Signal, QObject, QTemporaryFile, QIODevice, QByteArray

import subprocess
import logging
import time
import traceback
import atexit
import os
import psutil
from threading import Thread
from queue import Queue

from song_tree_widget_item import *
from const import APPLICATION

logger = logging.getLogger(APPLICATION)

PROCESSES = []

# make sure to stop all the ffmpeg processes from running
# if we close the application
def clean_up():
    for p in PROCESSES:
        try:
            process = psutil.Process(p.pid)
            for proc in process.children(recursive=True):
                proc.kill()
            process.kill()
        except:
            pass

atexit.register(clean_up)

class ProcessHandler(QObject):

    stdout = Signal(str)
    stderr = Signal(str)

    def __init__(self):
        super().__init__()

    def read_pipe(self, pipe, queue):
        try:
            with pipe:
                for line in iter(pipe.readline, b''):
                    queue.put((pipe, line.decode('utf-8')))
        finally:
            queue.put((None, None))

    def run(self, command):

        if os.name == "nt":
            p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW, shell=True)
        else:
            p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)

        PROCESSES.append(p)
        q = Queue()
        Thread(target=self.read_pipe, args=[p.stdout, q]).start()
        Thread(target=self.read_pipe, args=[p.stderr, q]).start()
        while True:
            while q.empty() or (item := q.get_nowait()) is None:
                time.sleep(0.01)
            pipe, line = item
            if pipe is None:
                break
            if pipe == p.stdout:
                self.stdout.emit(line)
            else:
                self.stderr.emit(line)
        error = p.wait() != 0
        PROCESSES.remove(p)
        return error

class RenderSongWorker(QObject):
    finished = Signal(bool)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, song: SongTreeWidgetItem):
        super().__init__()
        self.song = song

    def run(self):
        try:
            command_str = (self.song.get("commandString")).format(**self.song.to_dict())
            handler = ProcessHandler()
            handler.stderr.connect(self.error.emit)
            handler.stdout.connect(self.progress.emit)
            errors = handler.run(command_str)
            self.finished.emit(not errors)
        except Exception as e:
            self.error.emit(traceback.format_exc())
            self.finished.emit(False)

    def get_duration_ms(self):
        return self.song.get_duration_ms()

    def __str__(self):
        return self.song.get("fileOutput")

class CombineSongWorker(QObject):
    finished = Signal(bool)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, album: AlbumTreeWidgetItem):
        super().__init__()
        self.album = album

    def run(self):
        try:
            song_list = QTemporaryFile()
            song_list.open(QIODevice.WriteOnly | QIODevice.Append | QIODevice.Text)
            for song in self.album.getChildren():
                song_list.write(QByteArray("file 'file:{}'\n".format(song.get("fileOutput").replace("'", "'\\''"))))
            song_list.close()
            command_str = self.album.get("concatCommandString").format(
                                input_file_list=song_list.fileName(),
                                fileOutputPath=self.album.get("fileOutput"))
            handler = ProcessHandler()
            handler.stderr.connect(self.error.emit)
            handler.stdout.connect(self.progress.emit)
            errors = handler.run(command_str)
            for song in self.album.getChildren():
                os.remove(song.get("fileOutput"))
            self.finished.emit(not errors)
        except Exception as e:
            self.error.emit(traceback.format_exc())
            self.finished.emit(False)

    def get_duration_ms(self):
        return self.album.get_duration_ms()

    def __str__(self):
        return self.album.get("fileOutput")


class AlbumRenderHelper:

    def __init__(self, album: AlbumTreeWidgetItem, *args):
        self.album = album
        self.workers = set()
        self.renderer = None
        self.combine_worker = ""
        self.error = False

    def worker_done(self, worker, success):
        if worker in self.workers:
            self.workers.discard(worker)
            if not success:
                self.error = True
        if len(self.workers) == 0 and not self.error:
            # done rendering songs,
            # begin concatenation
            self.renderer.start_worker(self.combine_worker)

    def worker_error(self, worker, error):
        if worker in self.workers:
            # one of the songs could not be rendered,
            # cancel concatenation job
            self.error = True
            self.renderer.cancel_worker(self.combine_worker)

    def render(self, renderer):
        renderer.worker_done.connect(self.worker_done)
        renderer.worker_error.connect(self.worker_error)
        for song in self.album.getChildren():
            song.before_render()
            worker = renderer.add_render_song_job(song)
            self.workers.add(worker)
        self.combine_worker = renderer.combine_songs_into_album(self.album)
        self.renderer = renderer
        return self




class Renderer(QObject):

    # emit true on success, false on failure
    finished = Signal(dict)

    # worker name, worker progress (percentage)
    worker_progress = Signal(str, int)

    # worker name, worker error
    worker_error = Signal(str, str)

    # worker name
    worker_done = Signal(str, bool)

    def __init__(self):
        super().__init__()

        # threads to be worked on
        self.threads = []

        # workers -> threads dict
        self.workers = {}

        # array of album helpers so they
        # don't get garbage collected
        self.helpers = []

        # output file -> success
        self.results = {}

        self.working = False

        self.cancelled = False

    def _worker_progress(self, worker, progress):
        try:
            key, value = progress.strip().split("=")
            if key == "out_time_us":
                current_time_ms = int(value) // 1000
                total_time_ms = worker.get_duration_ms()
                progress = max(0, min(int((current_time_ms / total_time_ms) * 100), 100))
                self.worker_progress.emit(str(worker), progress)
        except:
            logger.warning("Could not parse worker_progress line: {}".format(progress))


    def worker_finished(self, worker, thread, success):
        self.results[str(worker)] = success
        if not self.cancelled:
            self.worker_done.emit(str(worker), success)
            thread.quit()
            worker.deleteLater()
            logger.debug("{} finished, success: {}".format(str(worker), success))

    def thread_finished(self, thread):
        if not self.cancelled:
            thread.deleteLater()
            if thread in self.threads:
                self.threads.remove(thread)
            for w, t in self.workers.items():
                if t == thread:
                    del self.workers[w]
                    break
            if len(self.threads) == 0:
                self.working = False
            if len(self.workers) == 0:
                self.finished.emit(self.results)
            # find first unstarted thread and start it
            for thread in self.threads:
                if not thread.isRunning():
                    thread.start()
                    self.working = True
                    break

    def start_worker(self, worker_name):
        # manually start a worker that wasn't created
        # with auto_start=True
        if worker_name in self.workers:
            thread = self.workers[worker_name]
            if thread not in self.threads:
                self.threads.append(thread)
                if not self.working:
                    self.render()

    def cancel_worker(self, worker_name):
        if worker_name in self.workers:
            thread = self.workers[worker_name]
            thread.quit()
            if thread in self.threads:
                self.threads.remove(thread)
            if len(self.threads) == 0:
                self.working = False
            del self.workers[worker_name]

    def add_worker(self, worker, auto_start=True):
        thread = QThread()
        if auto_start:
            self.threads.append(thread)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda success, worker=worker, thread=thread: self.worker_finished(worker, thread, success))
        thread.finished.connect(lambda thread=thread: self.thread_finished(thread))
        worker.error.connect(lambda error, worker=worker: self.worker_error.emit(str(worker), error))
        worker.progress.connect(lambda progress, worker=worker: self._worker_progress(worker, progress))
        self.workers[str(worker)] = thread

    def add_render_album_job(self, album: AlbumTreeWidgetItem):
        album.before_render()
        if album.childCount() == 0:
            return
        if album.get('albumPlaylist') == SETTINGS_VALUES.AlbumPlaylist.SINGLE:
            self.helpers.append(AlbumRenderHelper(album).render(self))
        elif album.get('albumPlaylist') == SETTINGS_VALUES.AlbumPlaylist.MULTIPLE:
            for song in album.getChildren():
                self.add_render_song_job(song)

    def add_render_song_job(self, song: SongTreeWidgetItem):
        song.before_render()
        worker = RenderSongWorker(song)
        self.add_worker(worker)
        return str(worker)

    def combine_songs_into_album(self, album: AlbumTreeWidgetItem):
        worker = CombineSongWorker(album)
        self.add_worker(worker, False)
        return str(worker)

    def render(self):
        self.working = True
        if len(self.workers) == 0:
            self.working = False
            self.finished.emit(self.results)
        for i in range(min(int(get_setting("maxProcesses")), len(self.threads))):
            self.threads[i].start()

    def cancel(self):
        clean_up()
        self.cancelled = True
        for worker in self.workers:
            if str(worker) not in self.results:
                self.results[str(worker)] = False
        self.finished.emit(self.results)