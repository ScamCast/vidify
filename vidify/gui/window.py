"""
This module implements the Qt interface and is where every other module is
put together.

The API and player modules are mixed using Qt events:
    * Position changes -> MainWindow.change_video_position(ms)
    * Status changes -> MainWindow.change_video_status(status)
    * Song changes -> MainWindow.play_video()
These events are generated inside the APIs.
"""

import time
import types
import logging
import importlib
from typing import Callable, Optional

from qtpy.QtWidgets import QWidget, QHBoxLayout
from qtpy.QtGui import QFontDatabase
from qtpy.QtCore import Qt, QTimer, QCoreApplication, Slot, QThread

from vidify import format_name
from vidify.api import APIData, get_api_data
from vidify.player import initialize_player
from vidify.config import Config
from vidify.youtube import YouTubeDLWorker
from vidify.lyrics import get_lyrics
from vidify.gui import Res, Colors
from vidify.gui.components import APISelection, APIConnecter


class MainWindow(QWidget):
    def __init__(self, config: Config) -> None:
        """
        Main window with the GUI and whatever player is being used.
        """

        super().__init__()
        self.setWindowTitle('vidify')

        # Setting the window to stay on top
        if config.stay_on_top:
            self.setWindowFlags(Qt.WindowStaysOnTopHint)

        # Setting the fullscreen and window size
        if config.fullscreen:
            self.showFullScreen()
        else:
            self.resize(config.width or 800, config.height or 600)

        # Loading the used fonts (Inter)
        font_db = QFontDatabase()
        for font in Res.fonts:
            font_db.addApplicationFont(font)

        # Initializing the player and saving the config object in the window.
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.player = initialize_player(config.player, config)
        logging.info("Using %s as the player", config.player)
        self.config = config

        # The API initialization is more complex. For more details, please
        # check the flow diagram in vidify.api. First we have to check if
        # the API is saved in the config:
        try:
            api_data = get_api_data(config.api)
        except KeyError:
            # Otherwise, the user is prompted for an API. After choosing one,
            # it will be initialized from outside this function.
            logging.info("API not found: prompting the user")
            self.API_selection = APISelection()
            self.layout.addWidget(self.API_selection)
            self.API_selection.api_chosen.connect(self.on_api_selection)
        else:
            logging.info("Using %s as the API", config.api)
            self.initialize_api(api_data)

    @Slot(str)
    def on_api_selection(self, api_str: str) -> None:
        """
        Method called when the API is selected with APISelection.
        The provided api string must be an existent entry
        inside the APIData enumeration.
        """

        # Removing the widget used to obtain the API string
        self.layout.removeWidget(self.API_selection)
        self.API_selection.setParent(None)
        self.API_selection.hide()
        del self.API_selection

        # Saving the API in the config
        self.config.api = api_str

        # Starting the API initialization
        self.initialize_api(APIData[api_str])

    def initialize_api(self, api_data: APIData) -> None:
        """
        Initializes an API with the information from APIData.
        """

        # The API may need interaction with the user to obtain credentials
        # or similar data. This function will already take care of the
        # rest of the initialization.
        if api_data.gui_init_fn is not None:
            fn = getattr(self, api_data.gui_init_fn)
            fn()
            return

        # Initializing the API with dependency injection.
        mod = importlib.import_module(api_data.module)
        cls = getattr(mod, api_data.class_name)
        self.api = cls()

        self.wait_for_connection(
            self.api.connect_api, message=api_data.connect_msg,
            event_loop_interval=api_data.event_loop_interval)

    def wait_for_connection(self, conn_fn: Callable[[], None],
                            message: Optional[str] = None,
                            event_loop_interval: int = 1000) -> None:

        """
        Creates an APIConnecter instance and waits for the API to be
        available, or times out otherwise.
        """

        self.event_loop_interval = event_loop_interval
        self.api_connecter = APIConnecter(
            conn_fn, message or "Waiting for connection")
        self.api_connecter.success.connect(self.on_conn_success)
        self.api_connecter.fail.connect(self.on_conn_fail)
        self.layout.addWidget(self.api_connecter)
        self.api_connecter.start()

    @Slot()
    def on_conn_fail(self) -> None:
        """
        If the API failed to connect, the app will be closed.
        """

        print("Timed out waiting for the connection")
        QCoreApplication.exit(1)

    @Slot(float)
    def on_conn_success(self, start_time: float) -> None:
        """
        Once the connection has been established correctly, the API can
        be started properly.
        """

        logging.info("Succesfully connected to the API")
        self.layout.removeWidget(self.api_connecter)
        del self.api_connecter

        # Initializing the optional audio synchronization extension, now
        # that there's access to the API's data. Note that this feature
        # is only available on Linux.
        if self.config.audiosync:
            from vidify.audiosync import AudiosyncWorker
            self.audiosync = AudiosyncWorker(self.api.player_name)
            self.audiosync.success.connect(self.on_audiosync_success)
            self.audiosync.failed.connect(self.on_audiosync_fail)

        # Loading the player
        self.setStyleSheet(f"background-color:{Colors.black};")
        self.layout.addWidget(self.player)
        self.play_video(self.api.artist, self.api.title, start_time)

        # Connecting to the signals generated by the API
        self.api.new_song_signal.connect(self.play_video)
        self.api.position_signal.connect(self.change_video_position)
        self.api.status_signal.connect(self.change_video_status)

        # Starting the event loop if it was initially passed as
        # a parameter.
        if self.event_loop_interval is not None:
            self.start_event_loop(self.api.event_loop,
                                  self.event_loop_interval)

    def start_event_loop(self, event_loop: Callable[[], None],
                         ms: int) -> None:
        """
        Starts a "manual" event loop with a timer every `ms` milliseconds.
        This is used with the SwSpotify API and the Web API to check every
        `ms` seconds if a change has happened, like if the song was paused.
        """

        logging.info("Starting event loop")
        timer = QTimer(self)

        # Qt doesn't accept a method as the parameter so it's converted
        # to a function.
        if isinstance(event_loop, types.MethodType):
            timer.timeout.connect(lambda: event_loop())
        else:
            timer.timeout.connect(event_loop)
        timer.start(ms)

    @Slot(bool)
    def change_video_status(self, is_playing: bool) -> None:
        """
        Slot used for API updates of the video status.
        """

        self.player.pause = not is_playing

        # If there is an audiosync thread running, this will pause the sound
        # recording and youtube downloading.
        if self.config.audiosync and self.audiosync.status != 'idle':
            self.audiosync.is_running = is_playing

    @Slot(int)
    def change_video_position(self, ms: int) -> None:
        """
        Slot used for API updates of the video position.
        """

        if not self.config.audiosync:
            self.player.position = ms

        # Audiosync is aborted if the position of the video changed, since
        # the audio being recorded won't make sense.
        if self.config.audiosync and self.audiosync.status != 'idle':
            self.audiosync.abort()

    @Slot(str, str, float)
    def play_video(self, artist: str, title: str, start_time: float) -> None:
        """
        Slot used to play a video. This is called when the API is first
        initialized from this GUI, and afterwards from the event loop handler
        whenever a new song is detected.

        If an error was detected when downloading the video, the default one
        is shown instead.

        Both audiosync and youtubedl work in separate threads to avoid
        blocking the GUI. This method will start both of them.
        """

        # Checking that the artist and title are valid first of all
        if self.api.artist in (None, '') and self.api.title in (None, ''):
            logging.info("The provided artist and title are empty.")
            self.on_youtubedl_fail()
            if self.config.audiosync:
                self.on_audiosync_fail()
            return

        # This delay is used to know the elapsed time until the video
        # actually starts playing, used in the audiosync feature.
        self.timestamp = start_time
        query = f"ytsearch:{format_name(artist, title)} Official Video"

        if self.config.audiosync:
            self.launch_audiosync(query)

        self.launch_youtubedl(query)

    def launch_audiosync(self, query: str) -> None:
        """
        Starts the audiosync thread, that will call either
        self.on_audiosync_success, or self.on_audiosync_fail once it's
        finished.

        First trying to stop the previous audiosync thread, as only
        one audiosync thread can be running at once.

        Note: QThread.start() is guaranteed to work once QThread.run()
        has returned. Thus, this will wait until it's done and launch
        the new one.
        """

        self.audiosync.abort()
        self.audiosync.wait()
        self.audiosync.youtube_title = query
        self.audiosync.start()
        logging.info("Started a new audiosync job")

    def launch_youtubedl(self, query: str) -> None:
        """
        Starts a YoutubeDL thread that will call either
        self.on_youtubedl_success or self.on_youtubedl_fail once it's done.
        """

        logging.info("Starting the youtube-dl thread")
        self.youtubedl = YouTubeDLWorker(
            query, self.config.debug, self.config.width, self.config.height)
        self.yt_thread = QThread()
        self.youtubedl.moveToThread(self.yt_thread)
        self.yt_thread.started.connect(self.youtubedl.get_url)
        self.youtubedl.success.connect(self.on_yt_success)
        self.youtubedl.fail.connect(self.on_youtubedl_fail)
        self.youtubedl.finish.connect(self.yt_thread.exit)
        self.yt_thread.start()

    @Slot()
    def on_youtubedl_fail(self) -> None:
        """
        If Youtube-dl for whatever reason failed to load the video, a fallback
        error video is shown, along with a message to let the user know what
        happened.
        """

        self.player.start_video(Res.default_video, self.api.is_playing)
        print("The video wasn't found, either because of an issue with your"
              " internet connection or because the provided data was invalid."
              " For more information, enable the debug mode.")

    @Slot(str)
    def on_yt_success(self, url: str) -> None:
        """
        Obtains the video URL from the Youtube-dl thread and starts playing
        the video. Also shows the lyrics if enabled. The position of the video
        isn't set if it's using audiosync, because this is done by the
        AudiosyncWorker thread.
        """

        self.player.start_video(url, self.api.is_playing)

        if not self.config.audiosync:
            try:
                self.player.position = self.api.position
            except NotImplementedError:
                self.player.position = 0

        # Finally, the lyrics are displayed. If the video wasn't found, an
        # error message is shown.
        if self.config.lyrics:
            print(get_lyrics(self.api.artist, self.api.title))

    @Slot()
    def on_audiosync_fail(self) -> None:
        """
        Currently, when audiosync fails, nothing happens.
        """

        logging.info("Audiosync module failed to return the lag")

    @Slot(int)
    def on_audiosync_success(self, lag: int) -> None:
        """
        Slot used after the audiosync function has finished. It sets the
        returned lag in milliseconds on the player.

        This assumes that the song wasn't paused until this issue is fixed:
        https://github.com/vidify/audiosync/issues/12
        """

        logging.info("Audiosync module returned %d ms", lag)

        # The current API position according to what's being recorded.
        playback_delay = round((time.time() - self.timestamp) * 1000) \
            - self.player.position
        lag += playback_delay

        # The user's custom audiosync delay. This is basically the time taken
        # until the module started recording (which may depend on the user
        # hardware and other things). Thus, it will almost always be a
        # negative value.
        lag += self.config.audiosync_calibration

        logging.info("Total delay is %d ms", lag)
        if lag > 0:
            self.player.position += lag
        elif lag < 0:
            # If a negative delay is larger than the current player position,
            # the player position is set to zero after the lag has passed
            # with a timer.
            if self.player.position < -lag:
                self.sync_timer = QTimer(self)
                self.sync_timer.singleShot(
                    -lag, lambda: self.change_video_position(0))
            else:
                self.player.position += lag

    def init_spotify_web_api(self) -> None:
        """
        SPOTIFY WEB API CUSTOM FUNCTION

        Note: the Tekore imports are done inside the functions so that
        Tekore isn't needed for whoever doesn't plan to use the Spotify
        Web API.
        """

        from vidify.api.spotify.web import get_token
        from vidify.gui.api.spotify_web import SpotifyWebPrompt

        token = get_token(self.config.refresh_token, self.config.client_id,
                          self.config.client_secret)

        if token is not None:
            # If the previous token was valid, the API can already start.
            logging.info("Reusing a previously generated token")
            self.start_spotify_web_api(token, save_config=False)
        else:
            # Otherwise, the credentials are obtained with the GUI. When
            # a valid auth token is ready, the GUI will initialize the API
            # automatically exactly like above. The GUI won't ask for a
            # redirect URI for now.
            logging.info("Asking the user for credentials")
            # The SpotifyWebPrompt handles the interaction with the user and
            # emits a `done` signal when it's done.
            self._spotify_web_prompt = SpotifyWebPrompt(
                self.config.client_id, self.config.client_secret,
                self.config.redirect_uri)
            self._spotify_web_prompt.done.connect(self.start_spotify_web_api)
            self.layout.addWidget(self._spotify_web_prompt)

    def start_spotify_web_api(self, token: 'RefreshingToken',
                              save_config: bool = True) -> None:
        """
        SPOTIFY WEB API CUSTOM FUNCTION

        Initializes the Web API, also saving them in the config for future
        usage (if `save_config` is true).
        """
        from vidify.api.spotify.web import SpotifyWebAPI

        logging.info("Initializing the Spotify Web API")

        # Initializing the web API
        self.api = SpotifyWebAPI(token)
        api_data = APIData['SPOTIFY_WEB']
        self.wait_for_connection(
            self.api.connect_api, message=api_data.connect_msg,
            event_loop_interval=api_data.event_loop_interval)

        # The obtained credentials are saved for the future
        if save_config:
            logging.info("Saving the Spotify Web API credentials")
            self.config.client_secret = self._spotify_web_prompt.client_secret
            self.config.client_id = self._spotify_web_prompt.client_id
            self.config.refresh_token = token.refresh_token

        # The credentials prompt widget is removed after saving the data. It
        # may not exist because start_spotify_web_api was called directly,
        # so errors are taken into account.
        try:
            self.layout.removeWidget(self._spotify_web_prompt)
            self._spotify_web_prompt.hide()
            del self._spotify_web_prompt
        except AttributeError:
            pass
