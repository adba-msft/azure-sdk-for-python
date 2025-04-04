# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
import abc
import base64
import json
import time
from uuid import uuid4
from typing import TYPE_CHECKING, List, Any, Iterable, Optional, Union, Dict, cast

from msal import TokenCache

from azure.core.pipeline import PipelineResponse
from azure.core.pipeline.policies import ContentDecodePolicy
from azure.core.pipeline.transport import HttpRequest
from azure.core.credentials import AccessTokenInfo
from azure.core.exceptions import ClientAuthenticationError
from .utils import get_default_authority, normalize_authority, resolve_tenant
from .aadclient_certificate import AadClientCertificate
from .._persistent_cache import _load_persistent_cache


if TYPE_CHECKING:
    from azure.core.pipeline import AsyncPipeline, Pipeline
    from azure.core.pipeline.policies import AsyncHTTPPolicy, HTTPPolicy, SansIOHTTPPolicy
    from azure.core.pipeline.transport import AsyncHttpTransport, HttpTransport

    PipelineType = Union[AsyncPipeline, Pipeline]
    PolicyType = Union[AsyncHTTPPolicy, HTTPPolicy, SansIOHTTPPolicy]
    TransportType = Union[AsyncHttpTransport, HttpTransport]

JWT_BEARER_ASSERTION = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class AadClientBase(abc.ABC):
    _POST = ["POST"]

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        authority: Optional[str] = None,
        cache: Optional[TokenCache] = None,
        cae_cache: Optional[TokenCache] = None,
        *,
        additionally_allowed_tenants: Optional[List[str]] = None,
        **kwargs: Any
    ) -> None:
        self._authority = normalize_authority(authority) if authority else get_default_authority()

        self._tenant_id = tenant_id
        self._client_id = client_id
        self._additionally_allowed_tenants = additionally_allowed_tenants or []
        self._pipeline = self._build_pipeline(**kwargs)

        self._cache = cache
        self._cae_cache = cae_cache
        self._cache_options = kwargs.pop("cache_persistence_options", None)
        if self._cache or self._cae_cache:
            self._custom_cache = True
        else:
            self._custom_cache = False
        self._is_adfs = self._tenant_id.lower() == "adfs"

    def _get_cache(self, **kwargs: Any) -> TokenCache:
        cache = self._cae_cache if kwargs.get("enable_cae") else self._cache
        if not cache:
            cache = self._initialize_cache(is_cae=bool(kwargs.get("enable_cae")))
        return cache

    def _initialize_cache(self, is_cae: bool = False) -> TokenCache:
        if self._cache_options:
            if is_cae:
                self._cae_cache = _load_persistent_cache(self._cache_options, is_cae)
            else:
                self._cache = _load_persistent_cache(self._cache_options, is_cae)
        else:
            if is_cae:
                self._cae_cache = TokenCache()
            else:
                self._cache = TokenCache()
        return cast(TokenCache, self._cae_cache if is_cae else self._cache)

    def get_cached_access_token(self, scopes: Iterable[str], **kwargs: Any) -> Optional[AccessTokenInfo]:
        tenant = resolve_tenant(
            self._tenant_id, additionally_allowed_tenants=self._additionally_allowed_tenants, **kwargs
        )

        cache = self._get_cache(**kwargs)
        for token in cache.search(
            TokenCache.CredentialType.ACCESS_TOKEN,
            target=list(scopes),
            query={"client_id": self._client_id, "realm": tenant},
        ):
            expires_on = int(token["expires_on"])
            if expires_on > int(time.time()):
                refresh_on = int(token["refresh_on"]) if "refresh_on" in token else None
                return AccessTokenInfo(
                    token["secret"], expires_on, token_type=token.get("token_type", "Bearer"), refresh_on=refresh_on
                )
        return None

    def get_cached_refresh_tokens(self, scopes: Iterable[str], **kwargs) -> List[Dict]:
        # Assumes all cached refresh tokens belong to the same user
        cache = self._get_cache(**kwargs)
        return list(cache.search(TokenCache.CredentialType.REFRESH_TOKEN, target=list(scopes)))

    @abc.abstractmethod
    def obtain_token_by_authorization_code(self, scopes, code, redirect_uri, client_secret=None, **kwargs):
        pass

    @abc.abstractmethod
    def obtain_token_by_jwt_assertion(self, scopes, assertion, **kwargs):
        pass

    @abc.abstractmethod
    def obtain_token_by_client_certificate(self, scopes, certificate, **kwargs):
        pass

    @abc.abstractmethod
    def obtain_token_by_client_secret(self, scopes, secret, **kwargs):
        pass

    @abc.abstractmethod
    def obtain_token_by_refresh_token(self, scopes, refresh_token, **kwargs):
        pass

    @abc.abstractmethod
    def obtain_token_on_behalf_of(self, scopes, client_credential, user_assertion, **kwargs):
        pass

    @abc.abstractmethod
    def _build_pipeline(self, **kwargs):
        pass

    def _process_response(self, response: PipelineResponse, request_time: int, **kwargs) -> AccessTokenInfo:
        content = response.context.get(
            ContentDecodePolicy.CONTEXT_NAME
        ) or ContentDecodePolicy.deserialize_from_http_generics(response.http_response)

        cache = self._get_cache(**kwargs)
        if response.http_request.body.get("grant_type") == "refresh_token":
            if content.get("error") == "invalid_grant":
                # the request's refresh token is invalid -> evict it from the cache
                cache_entries = list(
                    cache.search(
                        TokenCache.CredentialType.REFRESH_TOKEN,
                        query={"secret": response.http_request.body["refresh_token"]},
                    )
                )
                for invalid_token in cache_entries:
                    cache.remove_rt(invalid_token)
            if "refresh_token" in content:
                # Microsoft Entra ID returned a new refresh token -> update the cache entry
                cache_entries = list(
                    cache.search(
                        TokenCache.CredentialType.REFRESH_TOKEN,
                        query={"secret": response.http_request.body["refresh_token"]},
                    )
                )
                # If the old token is in multiple cache entries, the cache is in a state we don't
                # expect or know how to reason about, so we update nothing.
                if len(cache_entries) == 1:
                    cache.update_rt(cache_entries[0], content["refresh_token"])
                    del content["refresh_token"]  # prevent caching a redundant entry

        _raise_for_error(response, content)

        if "expires_on" in content:
            expires_on = int(content["expires_on"])
        elif "expires_in" in content:
            expires_on = request_time + int(content["expires_in"])
        else:
            _scrub_secrets(content)
            raise ClientAuthenticationError(message="Unexpected response from Microsoft Entra ID: {}".format(content))

        expires_in = int(content.get("expires_in") or expires_on - request_time)
        if "refresh_in" not in content and expires_in >= 7200:
            # MSAL TokenCache expects "refresh_in"
            content["refresh_in"] = expires_in // 2

        refresh_on = request_time + int(content["refresh_in"]) if "refresh_in" in content else None
        token = AccessTokenInfo(
            content["access_token"], expires_on, token_type=content.get("token_type", "Bearer"), refresh_on=refresh_on
        )

        # caching is the final step because 'add' mutates 'content'
        cache.add(
            event={
                "client_id": self._client_id,
                "response": content,
                "scope": response.http_request.body["scope"].split(),
                "token_endpoint": response.http_request.url,
            },
            now=request_time,
        )

        return token

    def _get_auth_code_request(
        self, scopes: Iterable[str], code: str, redirect_uri: str, client_secret: Optional[str] = None, **kwargs: Any
    ) -> HttpRequest:
        data = {
            "client_id": self._client_id,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
        }

        claims = _merge_claims_challenge_and_capabilities(
            ["CP1"] if kwargs.get("enable_cae") else [], kwargs.get("claims")
        )
        if claims:
            data["claims"] = claims
        if client_secret:
            data["client_secret"] = client_secret

        request = self._post(data, **kwargs)
        return request

    def _get_jwt_assertion_request(self, scopes: Iterable[str], assertion: str, **kwargs: Any) -> HttpRequest:
        data = {
            "client_assertion": assertion,
            "client_assertion_type": JWT_BEARER_ASSERTION,
            "client_id": self._client_id,
            "grant_type": "client_credentials",
            "scope": " ".join(scopes),
        }

        claims = _merge_claims_challenge_and_capabilities(
            ["CP1"] if kwargs.get("enable_cae") else [], kwargs.get("claims")
        )
        if claims:
            data["claims"] = claims

        request = self._post(data, **kwargs)
        return request

    def _get_client_certificate_assertion(self, certificate: AadClientCertificate, **kwargs: Any) -> str:
        now = int(time.time())
        headers = {"typ": "JWT"}
        if self._is_adfs:
            # Maintain backwards compatibility with older versions of ADFS.
            headers["alg"] = "RS256"
            headers["x5t"] = certificate.thumbprint
        else:
            headers["alg"] = "PS256"
            headers["x5t#S256"] = certificate.sha256_thumbprint

        jwt_header = json.dumps(headers).encode("utf-8")
        payload = json.dumps(
            {
                "jti": str(uuid4()),
                "aud": self._get_token_url(**kwargs),
                "iss": self._client_id,
                "sub": self._client_id,
                "nbf": now,
                "exp": now + (60 * 30),
            }
        ).encode("utf-8")
        jws = base64.urlsafe_b64encode(jwt_header) + b"." + base64.urlsafe_b64encode(payload)
        signature = certificate.sign_ps256(jws) if not self._is_adfs else certificate.sign_rs256(jws)
        jwt_bytes = jws + b"." + base64.urlsafe_b64encode(signature)
        return jwt_bytes.decode("utf-8")

    def _get_client_certificate_request(
        self, scopes: Iterable[str], certificate: AadClientCertificate, **kwargs: Any
    ) -> HttpRequest:
        assertion = self._get_client_certificate_assertion(certificate, **kwargs)
        return self._get_jwt_assertion_request(scopes, assertion, **kwargs)

    def _get_client_secret_request(self, scopes: Iterable[str], secret: str, **kwargs: Any) -> HttpRequest:
        data = {
            "client_id": self._client_id,
            "client_secret": secret,
            "grant_type": "client_credentials",
            "scope": " ".join(scopes),
        }

        claims = _merge_claims_challenge_and_capabilities(
            ["CP1"] if kwargs.get("enable_cae") else [], kwargs.get("claims")
        )
        if claims:
            data["claims"] = claims

        request = self._post(data, **kwargs)
        return request

    def _get_on_behalf_of_request(
        self,
        scopes: Iterable[str],
        client_credential: Union[str, AadClientCertificate, Dict[str, Any]],
        user_assertion: str,
        **kwargs: Any
    ) -> HttpRequest:
        data = {
            "assertion": user_assertion,
            "client_id": self._client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "requested_token_use": "on_behalf_of",
            "scope": " ".join(scopes),
        }

        claims = _merge_claims_challenge_and_capabilities(
            ["CP1"] if kwargs.get("enable_cae") else [], kwargs.get("claims")
        )
        if claims:
            data["claims"] = claims

        if isinstance(client_credential, AadClientCertificate):
            data["client_assertion"] = self._get_client_certificate_assertion(client_credential)
            data["client_assertion_type"] = JWT_BEARER_ASSERTION
        elif isinstance(client_credential, dict):
            func = client_credential["client_assertion"]
            data["client_assertion"] = func()
            data["client_assertion_type"] = JWT_BEARER_ASSERTION
        else:
            data["client_secret"] = client_credential

        request = self._post(data, **kwargs)
        return request

    def _get_refresh_token_request(self, scopes: Iterable[str], refresh_token: str, **kwargs: Any) -> HttpRequest:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
            "client_id": self._client_id,
            "client_info": 1,  # request Microsoft Entra ID include home_account_id in its response
        }
        client_secret = kwargs.pop("client_secret", None)
        if client_secret:
            data["client_secret"] = client_secret

        claims = _merge_claims_challenge_and_capabilities(
            ["CP1"] if kwargs.get("enable_cae") else [], kwargs.get("claims")
        )
        if claims:
            data["claims"] = claims

        request = self._post(data, **kwargs)
        return request

    def _get_refresh_token_on_behalf_of_request(
        self,
        scopes: Iterable[str],
        client_credential: Union[str, AadClientCertificate, Dict[str, Any]],
        refresh_token: str,
        **kwargs: Any
    ) -> HttpRequest:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
            "client_id": self._client_id,
            "client_info": 1,  # request Microsoft Entra ID include home_account_id in its response
        }
        claims = _merge_claims_challenge_and_capabilities(
            ["CP1"] if kwargs.get("enable_cae") else [], kwargs.get("claims")
        )
        if claims:
            data["claims"] = claims

        if isinstance(client_credential, AadClientCertificate):
            data["client_assertion"] = self._get_client_certificate_assertion(client_credential)
            data["client_assertion_type"] = JWT_BEARER_ASSERTION
        elif isinstance(client_credential, dict):
            func = client_credential["client_assertion"]
            data["client_assertion"] = func()
            data["client_assertion_type"] = JWT_BEARER_ASSERTION
        else:
            data["client_secret"] = client_credential
        request = self._post(data, **kwargs)
        return request

    def _get_token_url(self, **kwargs: Any) -> str:
        tenant = resolve_tenant(
            self._tenant_id, additionally_allowed_tenants=self._additionally_allowed_tenants, **kwargs
        )
        return "/".join((self._authority, tenant, "oauth2/v2.0/token"))

    def _post(self, data: Dict, **kwargs: Any) -> HttpRequest:
        url = self._get_token_url(**kwargs)
        return HttpRequest("POST", url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        # Remove the non-picklable entries
        if not self._custom_cache:
            del state["_cache"]
            del state["_cae_cache"]
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)
        # Re-create the unpickable entries
        if not self._custom_cache:
            self._cache = None
            self._cae_cache = None


def _merge_claims_challenge_and_capabilities(capabilities, claims_challenge):
    # Represent capabilities as {"access_token": {"xms_cc": {"values": capabilities}}}
    # and then merge/add it into incoming claims
    if not capabilities:
        return claims_challenge
    claims_dict = json.loads(claims_challenge) if claims_challenge else {}
    for key in ["access_token"]:
        claims_dict.setdefault(key, {}).update(xms_cc={"values": capabilities})
    return json.dumps(claims_dict)


def _scrub_secrets(response: Dict) -> None:
    for secret in ("access_token", "refresh_token"):
        if secret in response:
            response[secret] = "***"


def _raise_for_error(response: PipelineResponse, content: Dict) -> None:
    if "error" not in content:
        return

    _scrub_secrets(content)
    if "error_description" in content:
        message = "Microsoft Entra ID error '({}) {}'".format(content["error"], content["error_description"])
    else:
        message = "Microsoft Entra ID error '{}'".format(content)
    raise ClientAuthenticationError(message=message, response=response.http_response)
