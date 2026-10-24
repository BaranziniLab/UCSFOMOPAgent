"""Owner-private storage for durable jobs; handles pin every permission repair."""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import stat
import sys


class PrivateStorageError(ValueError):
    def __init__(self, message, code='private_storage_unavailable', kind='permission_denied'):
        super().__init__(message)
        self.code = code
        self.kind = kind


def _name(name):
    if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or '\\' in name:
        raise ValueError('Private storage requires one plain path component.')
    return name


def _clear_acl(fd, directory):
    if sys.platform == 'darwin':
        library = ctypes.CDLL(None, use_errno=True)
        library.acl_init.argtypes = [ctypes.c_int]
        library.acl_init.restype = ctypes.c_void_p
        library.acl_set_fd.argtypes = [ctypes.c_int, ctypes.c_void_p]
        library.acl_set_fd.restype = ctypes.c_int
        library.acl_free.argtypes = [ctypes.c_void_p]
        library.acl_free.restype = ctypes.c_int
        acl = library.acl_init(0)
        if not acl:
            raise OSError(ctypes.get_errno(), 'Cannot allocate an owner-private ACL.')
        try:
            if library.acl_set_fd(fd, acl) != 0:
                raise OSError(ctypes.get_errno(), 'Cannot enforce an owner-private ACL.')
        finally:
            library.acl_free(acl)
    elif sys.platform.startswith('linux'):
        names = ['system.posix_acl_access']
        if directory:
            names.append('system.posix_acl_default')
        for name in names:
            try:
                os.removexattr(fd, name)
            except OSError as error:
                if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                    raise
    else:
        raise OSError(errno.ENOTSUP, 'Owner-private ACL enforcement is unavailable on this platform.')


def _protect(fd, directory):
    information = os.fstat(fd)
    valid_type = stat.S_ISDIR(information.st_mode) if directory else stat.S_ISREG(information.st_mode)
    if not valid_type or information.st_uid != os.geteuid() or (not directory and information.st_nlink != 1):
        raise ValueError('Private job storage must contain only owned directories and regular files without links.')
    mode = 0o700 if directory else 0o600
    os.fchmod(fd, mode)
    _clear_acl(fd, directory)
    if stat.S_IMODE(os.fstat(fd).st_mode) != mode:
        raise OSError(errno.ENOTSUP, 'The filesystem does not enforce owner-private permissions.')


class PrivateDirectory:
    def __init__(self, path):
        if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
            raise OSError(errno.ENOTSUP, 'Secure private job storage is unavailable on this platform.')
        path = Path(path).expanduser().absolute()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path.parent.resolve(strict=True) / _name(path.name)
        self._fd = None
        parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                os.mkdir(self.path.name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        self._initialize(descriptor)

    @classmethod
    def _from_fd(cls, descriptor, path):
        instance = cls.__new__(cls)
        instance.path = path
        instance._fd = None
        instance._initialize(descriptor)
        return instance

    def _initialize(self, descriptor):
        self._fd = descriptor
        try:
            _protect(descriptor, True)
            information = os.fstat(descriptor)
            self.identity = (information.st_dev, information.st_ino)
            self._repair_tree(descriptor)
            self.verify()
        except BaseException:
            self.close()
            raise

    def _repair_tree(self, descriptor):
        # Enumeration and repairs remain beneath retained handles, including old stores.
        with os.scandir(descriptor) as entries:
            for entry in entries:
                try:
                    information = entry.stat(follow_symlinks=False)
                    directory = stat.S_ISDIR(information.st_mode)
                    if not directory and not stat.S_ISREG(information.st_mode):
                        raise ValueError('Private job storage contains a symbolic link or nonregular entry; recover it before retrying.')
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if directory:
                        flags |= os.O_DIRECTORY
                    child = os.open(entry.name, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
                try:
                    _protect(child, directory)
                    if directory:
                        self._repair_tree(child)
                finally:
                    os.close(child)

    def verify(self):
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            information = os.fstat(descriptor)
            if (information.st_dev, information.st_ino) != self.identity:
                raise ValueError('The private job directory changed; restore its original location before retrying.')
        finally:
            os.close(descriptor)
        _protect(self._fd, True)

    def child(self, name):
        self.verify()
        name = _name(name)
        try:
            os.mkdir(name, mode=0o700, dir_fd=self._fd)
        except FileExistsError:
            pass
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._fd)
        return self._from_fd(descriptor, self.path / name)

    def open_file(self, name, *, exclusive=False):
        self.verify()
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        if exclusive:
            flags |= os.O_EXCL
        descriptor = os.open(_name(name), flags, 0o600, dir_fd=self._fd)
        try:
            _protect(descriptor, False)
            return os.fdopen(descriptor, 'r+b')
        except BaseException:
            os.close(descriptor)
            raise

    def duplicate_fd(self):
        self.verify()
        return os.dup(self._fd)

    def close(self):
        if getattr(self, '_fd', None) is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def __del__(self):
        self.close()


if os.name == 'nt':
    from ._private_storage_windows import PrivateDirectory


def private_storage_call(action):
    try:
        return action()
    except (OSError, ValueError) as error:
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            raise PrivateStorageError(
                'Local job storage is full. Free space before explicitly resubmitting.',
                code='disk_full', kind='tool_failure',
            ) from error
        raise PrivateStorageError(
            'Private job storage could not be secured. Use an owned local directory '
            'with enforceable owner-only ACLs; recover symbolic/hard links or '
            'foreign-owned entries before retrying. No new job was started.'
        ) from error


def private_directory(path):
    return private_storage_call(lambda: PrivateDirectory(path))
