from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyQuery
from api.config import settings

api_key_query = APIKeyQuery(name="api_key", auto_error=True)


def get_api_key(api_key: str = Depends(api_key_query)):
    if api_key != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key"
        )
    return api_key
