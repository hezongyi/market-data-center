import uvicorn
import os


if __name__ == "__main__":
    uvicorn.run("data_center.api.app:app", host="127.0.0.1", port=int(os.getenv("DATACENTER_PORT", "18380")), reload=False)
