"""UCSFOMOPAgent database connector."""
__version__ = "0.3.2"
__all__ = ['create_ucsf_omop_server', 'main', 'UCSFOMOPConfig', '__version__']


def __getattr__(name):
    if name in __all__:
        from . import server
        return getattr(server, name)
    raise AttributeError(name)
