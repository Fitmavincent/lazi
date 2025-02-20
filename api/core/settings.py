from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # R2 Settings
    R2_ENDPOINT_URL: str
    R2_ACCESS_KEY_ID: str
    R2_SECRET_ACCESS_KEY: str
    R2_BUCKET_NAME: str
    R2_REGION: str = "auto"

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=True
    )

def get_settings():
    return Settings()
