import importlib
mods = [
    'src.agents.market_data_agent',
    'src.agents.news_sentiment_agent',
    'src.agents.technical_analysis_agent',
    'src.agents.risk_modeling_agent',
    'src.agents.performance_agent',
    'src.coordinator.trade_coordinator',
    'src.execution.order_execution_agent',
    'src.execution.position_monitor_agent',
    'ui.app',
]
for m in mods:
    try:
        importlib.import_module(m)
        print(f'OK  {m}')
    except Exception as e:
        print(f'ERR {m}')
        print(f'    {type(e).__name__}: {e}')
