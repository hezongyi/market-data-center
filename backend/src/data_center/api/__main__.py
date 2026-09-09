import uvicorn

from data_center.settings import Settings

if __name__ == "__main__":
    config = Settings()
    uvicorn.run("data_center.api.app:app", host=config.host, port=config.port, reload=False,
                access_log=False)
