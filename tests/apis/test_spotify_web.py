import os
import unittest
import unittest.mock

from spotivids import CURRENT_PLATFORM, Platform
from spotivids import Platform, CURRENT_PLATFORM


TRAVIS = "TRAVIS" in os.environ and os.environ["TRAVIS"] == "true"
SKIP_MSG = "Skipping this test as it won't work on the current system."


class SpotifyWebTest(unittest.TestCase):
    @unittest.skipIf(TRAVIS, SKIP_MSG)
    def test_simple(self):
        """
        The web credentials have to be already in the config file, including
        the auth token and the expiration date.
        """

        from spotivids.api.spotify.web import get_token, SpotifyWebAPI
        from spotivids.config import Config
        config = Config()
        with unittest.mock.patch('sys.argv', ['']):
            config.parse()
        token = get_token(config.refresh_token, config.client_id,
                          config.client_secret, config.redirect_uri)
        api = SpotifyWebAPI(token)
        api.connect_api()
        api._refresh_metadata()
        api.event_loop()


if __name__ == '__main__':
    unittest.main()