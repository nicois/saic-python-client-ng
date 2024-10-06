import logging

import httpx

from saic_ismart_client_ng.exceptions import SaicLogoutException
from saic_ismart_client_ng.listener import SaicApiListener
from saic_ismart_client_ng.model import SaicApiConfiguration
from saic_ismart_client_ng.net.client import AbstractSaicClient

LOG = logging.getLogger(__name__)


class SaicApiClient(AbstractSaicClient):
    def __init__(
            self,
            configuration: SaicApiConfiguration,
            listener: SaicApiListener = None
    ):
        super().__init__(configuration, listener, LOG)

    async def encrypt_request(self, modified_request: httpx.Request):
        if not self.user_token:
            raise SaicLogoutException("Client not authenticated, please call login first", return_code=401)
        await super().encrypt_request(modified_request)
