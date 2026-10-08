from .health_router import router as health_router

__all__ = ["health_router", "research_router"]


def __getattr__(name):
    if name == 'research_router':
        from .research_router import router
        return router
    raise AttributeError(name)
