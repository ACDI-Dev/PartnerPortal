import os
from datetime import timedelta


class Config:
  SECRET_KEY = os.environ.get(
      'FLASK_SECRET_KEY', 'a-very-secure-local-secret'
  )
  PREFERRED_URL_SCHEME = 'https'
  PERMANENT_SESSION_LIFETIME = timedelta(hours=18)

  # Cache Config
  REDIS_URL = os.environ.get('REDIS_URL')
  CACHE_TYPE = 'RedisCache' if REDIS_URL else 'SimpleCache'
  CACHE_REDIS_URL = REDIS_URL
  CACHE_DEFAULT_TIMEOUT = 14400