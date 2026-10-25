"""A requests session that identifies itself and paces its requests."""

import logging
import time

import requests

from .config import REQUEST_DELAY_SECONDS, USER_AGENT

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 502, 503, 504}


class PoliteSession:
    """Wraps ``requests.Session``: custom User-Agent, >= ``delay`` seconds between
    requests, and a couple of retries for transient failures."""

    def __init__(self, delay=REQUEST_DELAY_SECONDS, user_agent=USER_AGENT,
                 timeout=60, retries=2, sleep=time.sleep, clock=time.monotonic):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep
        self._clock = clock
        self._last_request = None
        self.request_count = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def _wait_turn(self):
        if self._last_request is not None:
            remaining = self.delay - (self._clock() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        attempt = 0
        while True:
            self._wait_turn()
            self._last_request = self._clock()
            self.request_count += 1
            log.debug("%s %s", method, url)
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.retries:
                    raise
                log.warning("%s %s failed (%s); retrying", method, url, exc)
            else:
                if resp.status_code not in RETRY_STATUSES or attempt >= self.retries:
                    return resp
                log.warning("%s %s returned HTTP %s; retrying", method, url, resp.status_code)
                resp.close()
            attempt += 1
            self._sleep(self.delay * (2 ** attempt))

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)
