import asyncio
import datetime
import logging
from abc import ABC
from dataclasses import asdict
from typing import Type, T, Optional, Any

import dacite
import httpx
import tenacity
from httpx._types import QueryParamTypes, HeaderTypes

from saic_ismart_client_ng.api.schema import LoginResp
from saic_ismart_client_ng.crypto_utils import sha1_hex_digest
from saic_ismart_client_ng.exceptions import SaicApiException, SaicApiRetryException, SaicLogoutException
from saic_ismart_client_ng.listener import SaicApiListener
from saic_ismart_client_ng.model import SaicApiConfiguration
from saic_ismart_client_ng.net.client.api import SaicApiClient
from saic_ismart_client_ng.net.client.login import SaicLoginClient

logger = logging.getLogger(__name__)


class AbstractSaicApi(ABC):
    def __init__(
            self,
            configuration: SaicApiConfiguration,
            listener: SaicApiListener = None,
    ):
        self.__configuration = configuration
        self.__login_client = SaicLoginClient(configuration, listener=listener)
        self.__api_client = SaicApiClient(configuration, listener=listener)
        self.__token_expiration = None

    @property
    def configuration(self) -> SaicApiConfiguration:
        return self.__configuration

    @property
    def login_client(self) -> SaicLoginClient:
        return self.__login_client

    @property
    def api_client(self) -> SaicApiClient:
        return self.__api_client

    @property
    def token_expiration(self) -> Optional[datetime.datetime]:
        return self.__token_expiration

    async def login(self) -> LoginResp:
        url = f"{self.configuration.base_uri}oauth/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        firebase_device_id = "cqSHOMG1SmK4k-fzAeK6hr:APA91bGtGihOG5SEQ9hPx3Dtr9o9mQguNiKZrQzboa-1C_UBlRZYdFcMmdfLvh9Q_xA8A0dGFIjkMhZbdIXOYnKfHCeWafAfLXOrxBS3N18T4Slr-x9qpV6FHLMhE9s7I6s89k9lU7DD"
        form_body = {
            "grant_type": "password",
            "username": self.configuration.username,
            "password": sha1_hex_digest(self.configuration.password),
            "scope": "all",
            "deviceId": f"{firebase_device_id}###europecar",
            "deviceType": "1",  # 2 for huawei
            "loginType": "2" if self.configuration.username_is_email else "1",
            "countryCode": "" if self.configuration.username_is_email else self.configuration.phone_country_code,
        }

        req = httpx.Request("POST", url, data=form_body, headers=headers)
        response = await self.login_client.client.send(req)
        result = await self.deserialize(response, LoginResp)
        # Update the user token
        self.api_client.user_token = result.access_token
        self.__token_expiration = datetime.datetime.now() + datetime.timedelta(seconds=result.expires_in)
        return result

    async def execute_api_call(
            self,
            method: str,
            path: str,
            body: Optional[Any] = None,
            out_type: Optional[Type[T]] = None,
            params: Optional[QueryParamTypes] = None,
            headers: Optional[HeaderTypes] = None,
    ) -> Optional[T]:
        url = f"{self.__configuration.base_uri}{path[1:] if path.startswith('/') else path}"
        json_body = asdict(body) if body else None
        req = httpx.Request(method, url, params=params, headers=headers, json=json_body)
        response = await self.api_client.client.send(req)
        return await self.deserialize(response, out_type)

    async def execute_api_call_with_event_id(
            self,
            method: str,
            path: str,
            body: Optional[Any] = None,
            out_type: Optional[Type[T]] = None,
            params: Optional[QueryParamTypes] = None,
            headers: Optional[HeaderTypes] = None,
    ) -> Optional[T]:
        @tenacity.retry(
            stop=tenacity.stop_after_delay(30),
            wait=tenacity.wait_fixed(self.__configuration.sms_delivery_delay),
            retry=saic_api_retry_policy,
            after=saic_api_after_retry,
            reraise=True,
        )
        async def execute_api_call_with_event_id_inner(*, event_id: str):
            actual_headers = headers or dict()
            actual_headers.update({'event-id': event_id})
            return await self.execute_api_call(
                method,
                path,
                body=body,
                out_type=out_type,
                params=params,
                headers=actual_headers
            )

        return await execute_api_call_with_event_id_inner(event_id='0')

    async def deserialize(self, response: httpx.Response, data_class: Optional[Type[T]]) -> Optional[T]:
        try:
            json_data = response.json()
            return_code = json_data.get('code', -1)
            error_message = json_data.get('message', 'Unknown error')
            logger.debug(f"Response code: {return_code} {response.text}")

            if return_code == 401:
                logger.error(f"API call return code is not acceptable: {return_code}: {response.text}")
                self.logout()
                if self.__configuration.relogin_delay:
                    logger.warning(f"Waiting since we got logged out: {return_code}: {response.text}")
                    await asyncio.sleep(self.__configuration.relogin_delay)
                logger.warning(f"Logging in since we got logged out")
                await self.login()
                raise SaicApiException(error_message, return_code=return_code)

            if return_code in (2, 3, 7):
                logger.error(f"API call return code is not acceptable: {return_code}: {response.text}")
                raise SaicApiException(error_message, return_code=return_code)

            if 'event-id' in response.headers and 'data' not in json_data:
                event_id = response.headers['event-id']
                logger.info(f"Retrying since we got even-id in headers: {event_id}, but no data")
                raise SaicApiRetryException(error_message, event_id=event_id, return_code=return_code)

            if return_code == 4:
                logger.info(f"API call asked us to retry: {return_code}: {response.text}")
                raise SaicApiRetryException(error_message, event_id='0', return_code=return_code)

            if return_code != 0:
                logger.error(
                    f"API call return code is not acceptable: {return_code}: {response.text}. Headers: {response.headers}"
                )
                raise SaicApiException(error_message, return_code=return_code)

            if data_class is None:
                return None
            elif 'data' in json_data:
                return dacite.from_dict(data_class, json_data['data'])
            else:
                raise SaicApiException(f"Failed to deserialize response, missing 'data' field: {response.text}")

        except SaicApiException as se:
            raise se
        except Exception as e:
            raise SaicApiException(f"Failed to deserialize response: {e}. Original json was {response.text}") from e

    def logout(self):
        self.api_client.user_token = None
        self.__token_expiration = None


def saic_api_after_retry(retry_state):
    wrapped_exception = retry_state.outcome.exception()
    if isinstance(wrapped_exception, SaicApiRetryException):
        if 'event_id' in retry_state.kwargs:
            logger.debug(f"Updating event_id to the newly obtained value {wrapped_exception.event_id}")
            retry_state.kwargs['event_id'] = wrapped_exception.event_id
        else:
            logger.debug(f"Retrying without an event_id")


def saic_api_retry_policy(retry_state):
    is_failed = retry_state.outcome.failed
    if is_failed:
        wrapped_exception = retry_state.outcome.exception()
        if isinstance(wrapped_exception, SaicApiRetryException):
            logger.debug("Retrying since we got SaicApiRetryException")
            return True
        elif isinstance(wrapped_exception, SaicLogoutException):
            logger.error("Retrying since we got logged out")
            return True
        elif isinstance(wrapped_exception, SaicApiException):
            logger.error("NOT Retrying since we got a generic exception")
            return False
        else:
            logger.error(f"Not retrying {retry_state.args} {wrapped_exception}")
            return False
    return False
