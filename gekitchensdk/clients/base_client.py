"""Base client for GE ERD APIs"""

import abc
from aiohttp import BasicAuth, ClientSession
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
import logging
from lxml import etree
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from ..erd import ErdCode, ErdCodeType
from ..exception import *
from ..ge_appliance import GeAppliance
from .const import (
    EVENT_APPLIANCE_INITIAL_UPDATE,
    EVENT_APPLIANCE_AVAILABLE,
    EVENT_APPLIANCE_UNAVAILABLE, 
    EVENT_CONNECTED, 
    EVENT_DISCONNECTED, 
    EVENT_STATE_CHANGED,
    MAX_RETRIES, 
    LOGIN_URL, 
    OAUTH2_CLIENT_ID, 
    OAUTH2_CLIENT_SECRET,
    OAUTH2_REDIRECT_URI
)
from .states import GeClientState

try:
    import re2 as re
except ImportError:
    import re

try:
    import ujson as json
except ImportError:
    import json

_LOGGER = logging.getLogger(__name__)


class GeBaseClient(metaclass=abc.ABCMeta):
    """Abstract base class for GE ERD APIs"""

    client_priority = 0  # Priority of this client class.  Higher is better.

    def __init__(self, username: str, password: str, event_loop: Optional[asyncio.AbstractEventLoop] = None):
        self.account_username = username
        self.account_password = password
        self._credentials = None  # type: Optional[Dict]
        self._session = None # type: Optional[ClientSession]

        self._access_token = None
        self._refresh_token = None
        self._token_expiration_time = datetime.now()

        self._state = GeClientState.INITIALIZING
        self._connected = False
        self._disconnect_requested = False
        self._retries_since_last_connect = -1
        self._loop = event_loop
        self._appliances = {}  # type: Dict[str, GeAppliance]
        self._initialize_event_handlers()

    @property
    def credentials(self) -> Optional[Dict]:
        return self._credentials

    @credentials.setter
    def credentials(self, credentials: Dict):
        self._credentials = credentials

    @property
    def appliances(self) -> Dict[str, GeAppliance]:
        return self._appliances

    @property
    def user_id(self) -> Optional[str]:
        try:
            return self.credentials['userId']
        except (TypeError, KeyError):
            raise GeNotAuthenticatedError

    @property
    def state(self) -> GeClientState:
        return self._state

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        return self._loop

    @property
    def connected(self) -> bool:
        """ Indicates whether the client is in a connected state """
        return self._state not in [GeClientState.DISCONNECTING, GeClientState.DISCONNECTED]
    
    @property
    def available(self) -> bool:
        """ Indicates whether the client is available for sending/receiving commands """
        return self._state == GeClientState.CONNECTED

    @property
    def event_handlers(self) -> Dict[str, List[Callable]]:
        return self._event_handlers

    async def async_event(self, event: str, *args, **kwargs):
        """Trigger event callbacks sequentially"""
        for cb in self.event_handlers[event]:
            asyncio.ensure_future(cb(*args, **kwargs), loop=self.loop)

    def add_event_handler(self, event: str, callback: Callable, disposable: bool = False):
        if disposable:
            raise NotImplementedError('Support for disposable callbacks not yet implemented')
        self.event_handlers[event].append(callback)

    def remove_event_handler(self, event: str, callback: Callable):
        try:
            self.event_handlers[event].remove(callable)
        except:
            _LOGGER.warn(f"could not remove event handler {event}-{callable}")

    def clear_event_handlers(self):
        self._initialize_event_handlers()

    async def async_get_credentials_and_run(self, session: ClientSession):
        """Do a full login flow and run the client."""
        await self.async_get_credentials(session)
        await self.async_run_client()

    async def async_run_client(self):
        self._disconnect_requested = False

        _LOGGER.info('Starting GE Appliances client')
        while not self._disconnect_requested:
            if self._retries_since_last_connect > MAX_RETRIES:
                break
            try:
                await self._async_run_client()
            except Exception as err:
                if(self._retries_since_last_connect == -1):
                    _LOGGER.warn(f'Unhandled exception on first connect attempt: {err}, disconnecting')
                    break
                _LOGGER.info(f'Unhandled exception while running client: {err}, ignoring and restarting')  
            finally:
                await self._set_state(GeClientState.DROPPED)
                if not self._disconnect_requested:
                    await self._set_state(GeClientState.WAITING)
                    _LOGGER.debug('Waiting before reconnecting')
                    asyncio.sleep(5)
                    _LOGGER.debug('Refreshing authentication before reconnecting')
                    try:
                        await self.async_do_refresh_login_flow()
                    except:
                        break
                self._retries_since_last_connect += 1

        #initiate the disconnection            
        self.disconnect()

    @abc.abstractmethod
    async def _async_run_client(self):
        """ Internal method to run the client """

    @abc.abstractmethod
    async def async_set_erd_value(self, appliance: GeAppliance, erd_code: ErdCodeType, erd_value: Any):
        """
        Send a new erd value to the appliance
        :param appliance: The appliance being updated
        :param erd_code: The ERD code to update
        :param erd_value: The new value to set
        """
        pass

    @abc.abstractmethod
    async def async_request_update(self, appliance: GeAppliance):
        """Request the appliance send a full state update"""
        pass

    async def async_get_credentials(self, session: ClientSession):
        """Get updated credentials"""
        self._session = session
        self.credentials = await self.async_do_full_login_flow()
        
    @abc.abstractmethod
    async def async_do_full_login_flow(self) -> Dict[str, str]:
        """Do the full login flow for this client"""
        pass

    @abc.abstractmethod
    async def async_do_refresh_login_flow(self) -> Dict[str, str]:
        """Do the refresh login flow for this client"""
        pass    

    async def _async_get_oauth2_token(self):
        """Hackily get an oauth2 token until I can be bothered to do this correctly"""

        await self._set_state(GeClientState.AUTHORIZING_OAUTH)

        params = {
            'client_id': OAUTH2_CLIENT_ID,
            'response_type': 'code',
            'access_type': 'offline',
            'redirect_uri': OAUTH2_REDIRECT_URI,
        }

        async with self._session.get(f'{LOGIN_URL}/oauth2/auth', params=params) as resp:
            if 400 <= resp.status < 500:
                raise GeAuthFailedError(await resp.text())
            if resp.status >= 500:
                raise GeGeneralServerError(await resp.text())
            resp_text = await resp.text()

        email_regex = (
            r'^\s*(\w+(?:(?:-\w+)|(?:\.\w+)|(?:\+\w+))*\@'
            r'[A-Za-z0-9]+(?:(?:\.|-)[A-Za-z0-9]+)*\.[A-Za-z0-9][A-Za-z0-9]+)\s*$'
        )
        clean_username = re.sub(email_regex, r'\1', self.account_username)

        etr = etree.HTML(resp_text)
        post_data = {
            i.attrib['name']: i.attrib['value']
            for i in etr.xpath("//form[@id = 'frmsignin']//input")
            if 'value' in i.keys()
        }
        post_data['username'] = clean_username
        post_data['password'] = self.account_password

        async with self._session.post(f'{LOGIN_URL}/oauth2/g_authenticate', data=post_data, allow_redirects=False) as resp:
            if 400 <= resp.status < 500:
                raise GeAuthFailedError(await resp.text())
            if resp.status >= 500:
                raise GeGeneralServerError(await resp.text())
            code = parse_qs(urlparse(resp.headers['Location']).query)['code'][0]

        post_data = {
            'code': code,
            'client_id': OAUTH2_CLIENT_ID,
            'client_secret': OAUTH2_CLIENT_SECRET,
            'redirect_uri': OAUTH2_REDIRECT_URI,
            'grant_type': 'authorization_code',
        }
        auth = BasicAuth(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET)
        async with self._session.post(f'{LOGIN_URL}/oauth2/token', data=post_data, auth=auth) as resp:
            if 400 <= resp.status < 500:
                raise GeAuthFailedError(await resp.text())
            if resp.status >= 500:
                raise GeGeneralServerError(await resp.text())
            oauth_token = await resp.json()
        try:
            self._access_token = oauth_token['access_token']
            self._token_expiration_time = datetime.now() + timedelta(seconds=(oauth_token['expires_in'] - 120))
            self._refresh_token = oauth_token['refresh_token']
        except KeyError:
            raise GeAuthFailedError(f'Failed to get a token: {oauth_token}')

    async def _async_refresh_oauth2_token(self, session: ClientSession):
        """ Refreshes an OAuth2 Token based on a refresh token """

        await self._set_state(GeClientState.AUTHORIZING_OAUTH)

        post_data = {
            'redirect_uri': OAUTH2_REDIRECT_URI,
            'grant_type': 'refresh_token',
            'refresh_token': self._refresh_token
        }
        auth = BasicAuth(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET)
        async with self._session.post(f'{LOGIN_URL}/oauth2/token', data=post_data, auth=auth) as resp:
            if 400 <= resp.status < 500:
                raise GeAuthFailedError(await resp.text())
            if resp.status >= 500:
                raise GeGeneralServerError(await resp.text())
            oauth_token = await resp.json()
        try:
            self._access_token = oauth_token['access_token']
            self._token_expiration_time = datetime.now() + timedelta(seconds=(oauth_token['expires_in'] - 120))
            self._refresh_token = oauth_token.get('refresh_token', self._refresh_token)
        except KeyError:
            raise GeAuthFailedError(f'Failed to get a token: {oauth_token}')

    async def _maybe_trigger_appliance_init_event(self, data: Tuple[GeAppliance, Dict[ErdCodeType, Any]]):
        """
        Trigger the appliance_got_type event if appropriate

        :param data: GeAppliance updated and the updates
        """
        appliance, state_changes = data
        if ErdCode.APPLIANCE_TYPE in state_changes and not appliance.initialized:
            _LOGGER.debug(f'Got initial appliance type for {appliance:s}')
            appliance.initialized = True
            await self.async_event(EVENT_APPLIANCE_INITIAL_UPDATE, appliance)

    async def _set_appliance_availability(self, appliance: GeAppliance, available: bool):
        if available and not appliance.available:
            appliance.set_available()
            await self.async_event(EVENT_APPLIANCE_AVAILABLE, appliance)
        elif not available and appliance.available:
            appliance.set_unavailable()
            await self.async_event(EVENT_APPLIANCE_UNAVAILABLE, appliance)

    async def _set_state(self, new_state: GeClientState) -> bool:
        """ Indicate that the state changed and raise an event """
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            await self.async_event(EVENT_STATE_CHANGED, old_state, new_state)
            return True
        return False            

    def _initialize_event_handlers(self):
        self._event_handlers = defaultdict(list)  # type: Dict[str, List[Callable]]
        self.add_event_handler(EVENT_STATE_CHANGED, self._on_state_change)
        pass

    async def _on_state_change(self, old_state: GeClientState, new_state: GeClientState):
        _LOGGER.debug(f'Client changed state: {old_state} to {new_state}')

        if new_state == GeClientState.CONNECTED:
            await self.async_event(EVENT_CONNECTED)
        if new_state == GeClientState.DISCONNECTED:
            await self.async_event(EVENT_DISCONNECTED)

    async def disconnect(self):
        """Disconnect and cleanup."""
        _LOGGER.info("Disconnecting")
        await self._set_state(GeClientState.DISCONNECTING)         
        self._disconnect_requested = True
        self._connected = False
        self._disconnect()
        await self._set_state(GeClientState.DISCONNECTED) 

    async def _set_connected(self):
        self._retries_since_last_connect = -1
        await self._set_state(GeClientState.CONNECTED)

    @abc.abstractmethod
    def _disconnect(self) -> None:
        pass
