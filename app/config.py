"""配置读取。所有可调参数集中在这里，避免散落在各处。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    deepseek_api: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    # 推荐要有点变化，温度别太低
    deepseek_temperature: float = 0.7

    # 推荐的菜数
    dish_count: int = 4
    # 粗筛后取前几条去补投币数。搜索接口不返回投币，每条候选要多打一次 view
    # 接口，所以这个值直接决定额外请求数：菜数 × 该值
    coin_lookup_top: int = 6
    # 标题与菜名的字符覆盖率下限，低于此值视为跑题视频
    min_relevance: float = 0.6
    # 超过这个时长的多半是合集/直播回放，不是单个菜谱
    max_duration_seconds: int = 20 * 60
    # B 站请求超时与并发上限
    request_timeout: float = 15.0
    max_concurrency: int = 6

    # 直接运行 app/main.py 时的监听地址。想从局域网其它设备访问就改成 0.0.0.0
    app_host: str = "127.0.0.1"
    app_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
