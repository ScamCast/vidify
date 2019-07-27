import os
import sys
import time

import vlc
import dbus
import youtube_dl
import spotipy
import spotipy.util

# Asynchronous calls to dbus loops
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
DBusGMainLoop(set_as_default=True)


# Prints formatted logs to the console if debug is True
def log(msg, debug = True):
    if debug:
        print("\033[92m>> " + msg + "\033[0m")


# Prints formatted errors to the console
def error(msg):
    print("\033[91m[ERROR]: " + msg + "\033[0m")
    sys.exit(1)


# VLC window playing the videos
class VLCWindow:
    def __init__(self, debug = False, vlc_args = "", fullscreen = False):
        self._debug = debug
        self._fullscreen = fullscreen

        # VLC Instance
        if self._debug: vlc_args += " --verbose 1"
        else: vlc_args += " --quiet"
        try:
            self._instance = vlc.Instance(vlc_args)
        except NameError:
            error("VLC is not installed")
        self.video_player = self._instance.media_player_new()

    # Plays/Pauses the VLC player
    def play(self):
        self.video_player.play()

    def pause(self):
        self.video_player.pause()

    def toggle_pause(self):
        if self.video_player.is_playing():
            self.pause()
        else:
            self.play()

    # Getting the video url with youtube-dl for VLC to play
    def get_url(self, name, ydl_opts):
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info("ytsearch:" + name, download=False)
        log(info['entries'][0]['url'])
        return info['entries'][0]['url']

    # Starts a new video on the VLC player
    def start_video(self, url):
        log("Starting video", self._debug)
        # Media instance
        Media = self._instance.media_new(url)
        Media.get_mrl()
        # Player instance
        self.video_player.set_media(Media)
        self.video_player.set_fullscreen(self._fullscreen)

    # Set the position of the VLC media playing
    def set_position(self, ms):
        self.video_player.set_time(ms)


# Dbus player with the Spotify properties (on Linux)
class DbusPlayer:
    def __init__(self, debug = False, vlc_args = "", fullscreen = False):
        # Main player properties
        self.artist = ""
        self.title = ""
        self.status = 'stopped'
        self._debug = debug
        self.player = VLCWindow(
                debug = debug,
                vlc_args = vlc_args,
                fullscreen = fullscreen
        )

        # DBus internal properties
        self._session_bus = dbus.SessionBus()
        self._bus_name = "org.mpris.MediaPlayer2.spotify"
        self._disconnecting = False
        try:
            self._obj = self._session_bus.get_object(self._bus_name, '/org/mpris/MediaPlayer2')
        except dbus.exceptions.DBusException as e:
            error("No spotify session running")
        self._properties_interface = dbus.Interface(self._obj, dbus_interface="org.freedesktop.DBus.Properties")
        self._introspect_interface = dbus.Interface(self._obj, dbus_interface="org.freedesktop.DBus.Introspectable")
        self._media_interface      = dbus.Interface(self._obj, dbus_interface='org.mpris.MediaPlayer2')
        self._player_interface     = dbus.Interface(self._obj, dbus_interface='org.mpris.MediaPlayer2.Player')
        self._introspect = self._introspect_interface.get_dbus_method('Introspect', dbus_interface=None)
        self._loop = GLib.MainLoop()
        self._signals = {}

        self._refresh_status()
        self._refresh_metadata()
        self.do_connect()
    
    # Connects to the dbus signals
    def do_connect(self):
        log("Connecting", self._debug)
        if self._disconnecting is False:
            introspect_xml = self._introspect(self._bus_name, '/')
            if 'TrackMetadataChanged' in introspect_xml:
                self._signals['track_metadata_changed'] = self._session_bus.add_signal_receiver(self._refresh_metadata, 'TrackMetadataChanged', self._bus_name)
            self._properties_interface.connect_to_signal('PropertiesChanged', self._on_properties_changed)

    # Disconnects from the dbus signals
    def do_disconnect(self):
        log("Disconnecting", self._debug)
        self._disconnecting = True
        for signal_name, signal_handler in list(self._signals.items()):
            signal_handler.remove()
            del self._signals[signal_name]

    # Waits for changes in dbus properties
    def wait(self):
        log("Starting loop", self._debug)
        self._loop.run()

    # Refreshes the status of the player (play/pause)
    def _refresh_status(self):
        # Some clients (VLC) will momentarily create a new player before removing it again
        # so we can't be sure the interface still exists
        try:
            self.status = str(self._properties_interface.Get('org.mpris.MediaPlayer2.Player', 'PlaybackStatus')).lower()
        except dbus.exceptions.DBusException as e:
            error(e)
            self.do_disconnect()

    # Returns the artist and title out of a raw metadata object
    def _formatted_metadata(self, metadata):
        return metadata['xesam:artist'][0], metadata['xesam:title']

    # Assigns the new metadata to the class's properties
    def _assign_metadata(self):
        _metadata = self._properties_interface.Get("org.mpris.MediaPlayer2.Player", "Metadata")
        try:
            self.artist, self.title = self._formatted_metadata(_metadata)
        except IndexError:
            error("No song currently playing")
            self.do_disconnect()

    # Refreshes the metadata of the player (artist, title)
    def _refresh_metadata(self):
        # Some clients (VLC) will momentarily create a new player before removing it again
        # so we can't be sure the interface still exists
        try:
            self._assign_metadata()
        except dbus.exceptions.DBusException as e:
            error(e)
            self.do_disconnect()

    # Function called asynchronously from dbus on property changes
    def _on_properties_changed(self, interface, properties, signature):
        # Format the new metadata. If it's different, break the loop
        if dbus.String('Metadata') in properties:
            _artist, _title = self._formatted_metadata(properties[dbus.String('Metadata')])
            if _artist != self.artist or _title != self.title:
                log("New video", self._debug)
                self._assign_metadata()
                self._loop.quit()

        # Paused/Played
        if dbus.String('PlaybackStatus') in properties:
            status = str(properties[dbus.String('PlaybackStatus')]).lower()
            if status != self.status:
                log("Paused/Played video", self._debug)
                self.status = status
                self.player.toggle_pause()



class WebPlayer:
    def __init__(self, username, client_id, client_secret, redirect_uri, debug = False, vlc_args = "", fullscreen = False):
        # Main player properties
        self.artist = ""
        self.title = ""
        self.position = 0
        self.status = 'stopped'
        self._debug = debug
        self.player = VLCWindow(
                debug = debug,
                vlc_args = vlc_args,
                fullscreen = fullscreen
        )

        # Checking that all parameters are passed
        if not username: error("You must pass your username as an argument. Example: --username='yourname'")
        if not client_id: error("You must pass your client ID as an argument. Example: --client-id='TODO'")
        if not client_secret: error("You must pass your client secret as an argument. Example: --client-secret='TODO'")

        # Creation of the Spotify token
        self._token = spotipy.util.prompt_for_user_token(
                username,
                scope = 'user-read-currently-playing',
                client_id = client_id,
                client_secret = client_secret,
                redirect_uri = redirect_uri
        )
        if self._token:
            self._spotify = spotipy.Spotify(auth = self._token)
        else:
            error("Can't get token for " + username)

        # Initializes variables
        self._spotify.trace = False
        self._refresh_metadata()

    # Returns the artist and title out of a raw metadata object
    def _formatted_metadata(self, metadata):
        return metadata['item']['artists'][0]['name'], metadata['item']['name'], metadata['progress_ms']

    # Refreshes the metadata and status of the player (artist, title, position)
    def _refresh_metadata(self):
        _metadata = self._spotify.current_user_playing_track()
        self.artist, self.title, self.position = self._formatted_metadata(_metadata)
        # Consistency with the status variable
        is_playing = _metadata['is_playing']
        if is_playing: self.status = "playing"
        else: self.status = "stopped"

    # Returns the position of the player
    def get_position(self):
        self._refresh_metadata()
        return self.position

    # Loop that waits until a new song is played
    def wait(self):
        while True:
            time.sleep(1)
            _artist = self.artist
            _title = self.title
            _status = self.status
            _position = self.position
            self._refresh_metadata()
            if self.status != _status:
                log("Paused/Played video", self._debug)
                self.player.toggle_pause()
            # Changes position if it's more than 3 seconds (1 with margin) or less than 0
            diff = self.position - _position
            if diff >= 3000 or diff < 0:
                log("Position changed", self._debug)
                self.player.set_position(self.position)
            if self.artist != _artist or self.title != _title:
                log("Song changed", self._debug)
                break
