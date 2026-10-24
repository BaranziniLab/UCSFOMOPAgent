"""Handle-checked, current-owner-only storage for Windows durable jobs."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import errno
import os
from pathlib import Path

if os.name != 'nt':
    raise ImportError('The Windows private storage backend requires Windows.')

import msvcrt


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_security = ctypes.WinDLL('advapi32', use_last_error=True)
_POINTER = ctypes.c_void_p
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_READ_ATTRIBUTES = 0x80
_LIST_DIRECTORY = 1
_SHARE_READ_WRITE = 3
_SHARE_DELETE = 4
_DIRECTORY = 0x10
_REPARSE = 0x400
_BACKUP_SEMANTICS = 0x02000000
_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE = _POINTER(-1).value
_OWNER_INFO = 1
_DACL_INFO = 4
_PROTECTED_DACL_INFO = 0x80000000
_DACL_PROTECTED = 0x1000
_FILE_ALL_ACCESS = 0x001F01FF
_MUTATION_RIGHTS = 0x510D0156
_KNOWN_FILE_RIGHTS = _FILE_ALL_ACCESS | 0xF1000000


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', _POINTER),
                ('bInheritHandle', wintypes.BOOL)]


class _SidAttributes(ctypes.Structure):
    _fields_ = [('Sid', _POINTER), ('Attributes', wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [('User', _SidAttributes)]


class _FileInformation(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]


class _AclHeader(ctypes.Structure):
    _fields_ = [('revision', wintypes.BYTE), ('reserved', wintypes.BYTE),
                ('size', wintypes.WORD), ('count', wintypes.WORD),
                ('reserved2', wintypes.WORD)]


class _AceHeader(ctypes.Structure):
    _fields_ = [('kind', wintypes.BYTE), ('flags', wintypes.BYTE), ('size', wintypes.WORD)]


_kernel.GetCurrentProcess.argtypes = []
_kernel.GetCurrentProcess.restype = wintypes.HANDLE
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.restype = wintypes.BOOL
_kernel.LocalFree.argtypes = [_POINTER]
_kernel.LocalFree.restype = _POINTER
_kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(_SecurityAttributes), wintypes.DWORD,
                               wintypes.DWORD, wintypes.HANDLE]
_kernel.CreateFileW.restype = wintypes.HANDLE
_kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]
_kernel.CreateDirectoryW.restype = wintypes.BOOL
_kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInformation)]
_kernel.GetFileInformationByHandle.restype = wintypes.BOOL
_kernel.GetFileType.argtypes = [wintypes.HANDLE]
_kernel.GetFileType.restype = wintypes.DWORD
_security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_security.OpenProcessToken.restype = wintypes.BOOL
_security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, _POINTER,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_security.GetTokenInformation.restype = wintypes.BOOL
_security.ConvertSidToStringSidW.argtypes = [_POINTER, ctypes.POINTER(_POINTER)]
_security.ConvertSidToStringSidW.restype = wintypes.BOOL
_security.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_POINTER)]
_security.ConvertStringSidToSidW.restype = wintypes.BOOL
_security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_POINTER), ctypes.POINTER(wintypes.DWORD)]
_security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
_security.GetSecurityDescriptorDacl.argtypes = [
    _POINTER, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(_POINTER), ctypes.POINTER(wintypes.BOOL)]
_security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
_security.GetSecurityDescriptorControl.argtypes = [
    _POINTER, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
_security.GetSecurityDescriptorControl.restype = wintypes.BOOL
_security.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                    ctypes.POINTER(_POINTER), ctypes.POINTER(_POINTER),
                                    ctypes.POINTER(_POINTER), ctypes.POINTER(_POINTER),
                                    ctypes.POINTER(_POINTER)]
_security.GetSecurityInfo.restype = wintypes.DWORD
_security.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                    _POINTER, _POINTER, _POINTER, _POINTER]
_security.SetSecurityInfo.restype = wintypes.DWORD
_security.IsValidSid.argtypes = [_POINTER]
_security.IsValidSid.restype = wintypes.BOOL
_security.GetLengthSid.argtypes = [_POINTER]
_security.GetLengthSid.restype = wintypes.DWORD
_security.EqualSid.argtypes = [_POINTER, _POINTER]
_security.EqualSid.restype = wintypes.BOOL
_security.GetAce.argtypes = [_POINTER, wintypes.DWORD, ctypes.POINTER(_POINTER)]
_security.GetAce.restype = wintypes.BOOL


def _error():
    return ctypes.WinError(ctypes.get_last_error())


def _name(name):
    if (not isinstance(name, str) or name in ('', '.', '..')
            or any(character in name for character in '/\\:\0')
            or name.endswith((' ', '.'))):
        raise ValueError('Private storage requires one ordinary Windows path component.')
    stem = name.split('.')[0].upper()
    if stem in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} or (
            len(stem) == 4 and stem[:3] in ('COM', 'LPT') and stem[3] in '123456789¹²³'):
        raise ValueError('Windows device names cannot be private storage entries.')
    return name


def _absolute(path):
    value = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if value.startswith(('\\\\?\\', '\\\\.\\')):
        raise ValueError('Windows device namespace paths cannot be private job storage.')
    result = Path(value)
    if not result.anchor:
        raise ValueError('Private storage requires an absolute directory.')
    for component in result.parts[1:]:
        _name(component)
    if result == Path(result.anchor):
        raise ValueError('A drive or share root cannot be private job storage.')
    return result


def _close(handle):
    if handle is not None and handle != _INVALID_HANDLE:
        _kernel.CloseHandle(handle)


def _open(path, directory, *, repair=True):
    access = _READ_CONTROL | _READ_ATTRIBUTES
    if directory:
        access |= _LIST_DIRECTORY
    if repair:
        access |= _WRITE_DAC
    # Retained directory handles prevent pathname replacement. File metadata
    # handles allow journal deletion while still binding repairs to the object.
    share = _SHARE_READ_WRITE | (0 if directory else _SHARE_DELETE)
    handle = _kernel.CreateFileW(str(path), access, share, None, 3,
                                 _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS, None)
    if handle == _INVALID_HANDLE:
        raise _error()
    return handle


def _information(handle):
    result = _FileInformation()
    if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(result)):
        raise _error()
    return result


def _identity(information):
    return information.volume, (information.index_high << 32) | information.index_low


@contextmanager
def _security_info(handle):
    owner, dacl, descriptor = _POINTER(), _POINTER(), _POINTER()
    code = _security.GetSecurityInfo(handle, 1, _OWNER_INFO | _DACL_INFO,
                                     ctypes.byref(owner), None, ctypes.byref(dacl),
                                     None, ctypes.byref(descriptor))
    if code:
        raise ctypes.WinError(code)
    try:
        yield owner, dacl, descriptor
    finally:
        _kernel.LocalFree(descriptor)


class _Security:
    def __init__(self):
        self._descriptors = []
        self._owned_sids = []
        token = wintypes.HANDLE()
        if not _security.OpenProcessToken(_kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise _error()
        try:
            size = wintypes.DWORD()
            _security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not ctypes.sizeof(_TokenUser) <= size.value <= 65536:
                raise OSError(errno.ENOTSUP, 'Cannot obtain the Windows process owner SID.')
            self._user = ctypes.create_string_buffer(size.value)
            if not _security.GetTokenInformation(token, 1, self._user, size, ctypes.byref(size)):
                raise _error()
            self.sid = ctypes.cast(self._user, ctypes.POINTER(_TokenUser)).contents.User.Sid
            size = wintypes.DWORD()
            _security.GetTokenInformation(token, 4, None, 0, ctypes.byref(size))
            if not ctypes.sizeof(_POINTER) <= size.value <= 65536:
                raise OSError(errno.ENOTSUP, 'Cannot obtain the Windows process default owner SID.')
            self._owner = ctypes.create_string_buffer(size.value)
            if not _security.GetTokenInformation(token, 4, self._owner, size, ctypes.byref(size)):
                raise _error()
            self.owner_sid = ctypes.cast(self._owner, ctypes.POINTER(_POINTER)).contents.value
        finally:
            _close(token)
        if not _security.IsValidSid(self.sid) or not _security.IsValidSid(self.owner_sid):
            raise ValueError('A Windows process ownership SID is invalid.')
        string = _POINTER()
        if not _security.ConvertSidToStringSidW(self.sid, ctypes.byref(string)):
            raise _error()
        try:
            self.sid_text = ctypes.wstring_at(string)
        finally:
            _kernel.LocalFree(string)
        try:
            self.system_sid = self._sid('S-1-5-18')
            self.administrators_sid = self._sid('S-1-5-32-544')
            self.directory = self._descriptor('OICI')
            self.file = self._descriptor('')
        except BaseException:
            self.close()
            raise

    def _sid(self, text):
        sid = _POINTER()
        if not _security.ConvertStringSidToSidW(text, ctypes.byref(sid)):
            raise _error()
        self._owned_sids.append(sid)
        if not _security.IsValidSid(sid):
            raise ValueError('A well-known Windows security SID is invalid.')
        return sid

    def _owner_allowed(self, owner):
        if _security.EqualSid(owner, self.sid):
            return True
        return bool(_security.EqualSid(owner, self.owner_sid) and any(
            _security.EqualSid(self.owner_sid, privileged)
            for privileged in (self.system_sid, self.administrators_sid)
        ))

    def _descriptor(self, inheritance):
        descriptor = _POINTER()
        sddl = f'O:{self.sid_text}D:P(A;{inheritance};FA;;;{self.sid_text})'
        if not _security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise _error()
        self._descriptors.append(descriptor)
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), _POINTER()
        if not _security.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                  ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise _error()
        if not present or not dacl:
            raise ValueError('An owner-only Windows DACL could not be constructed.')
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        return attributes, dacl

    def validate(self, handle, directory, *, owner=True):
        information = _information(handle)
        if (_kernel.GetFileType(handle) != 1 or information.attributes & _REPARSE
                or bool(information.attributes & _DIRECTORY) != directory
                or (not directory and information.links != 1)):
            raise ValueError('Private job storage requires ordinary files and directories without reparse points or hard links.')
        if owner:
            with _security_info(handle) as (actual_owner, unused, descriptor):
                # Elevated tokens give SQLite journals an administrator owner.
                # A non-admin group owner would give other ordinary members
                # implicit WRITE_DAC even after an owner-only DACL repair.
                if (not actual_owner or not _security.IsValidSid(actual_owner)
                        or not self._owner_allowed(actual_owner)):
                    raise ValueError('Private job storage must be owned by the Windows account or its exact administrator process default owner.')
        return _identity(information)

    @staticmethod
    def _unsafe_legacy_directory():
        from .private_storage import PrivateStorageError
        return PrivateStorageError(
            'Existing Windows job storage is writable by another ordinary account, '
            'or its ACL cannot be safely inspected. Stop all processes using the store, '
            'move it to an account-owned local directory and have its owner or administrator '
            'remove other accounts\' write/delete/change-permission access throughout the tree '
            'before retrying. Automatic ACL repair was refused; no existing ACL was changed.',
            code='private_storage_requires_quiescent_repair', kind='permission_denied',
        )

    def preflight_directory(self, handle):
        self.validate(handle, True)
        with _security_info(handle) as (owner, dacl, descriptor):
            if not dacl:
                raise self._unsafe_legacy_directory()
            header = ctypes.cast(dacl, ctypes.POINTER(_AclHeader)).contents
            if header.revision not in (2, 4):
                raise self._unsafe_legacy_directory()
            for index in range(header.count):
                ace = _POINTER()
                if not _security.GetAce(dacl, index, ctypes.byref(ace)):
                    raise self._unsafe_legacy_directory()
                entry = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
                if entry.kind not in (0, 1) or entry.flags & ~0x1F or entry.size < 16:
                    raise self._unsafe_legacy_directory()
                sid = _POINTER(ace.value + 8)
                if not _security.IsValidSid(sid) or 8 + _security.GetLengthSid(sid) > entry.size:
                    raise self._unsafe_legacy_directory()
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                if mask & ~_KNOWN_FILE_RIGHTS:
                    raise self._unsafe_legacy_directory()
                if entry.kind == 1 or entry.flags & 8:
                    continue
                trusted = any(_security.EqualSid(sid, safe) for safe in (
                    self.sid, self.system_sid, self.administrators_sid,
                ))
                # Conservatively refuse a foreign mutation allow ACE even if
                # a separate deny ACE might narrow its effective rights.
                if not trusted and mask & _MUTATION_RIGHTS:
                    raise self._unsafe_legacy_directory()

    def assert_private(self, handle, directory):
        self.validate(handle, directory)
        with _security_info(handle) as (owner, dacl, descriptor):
            control, revision = wintypes.WORD(), wintypes.DWORD()
            if not _security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise _error()
            if not dacl or not control.value & _DACL_PROTECTED or ctypes.cast(dacl, ctypes.POINTER(_AclHeader)).contents.count != 1:
                raise OSError(errno.ENOTSUP, 'The filesystem did not enforce the protected owner-only Windows DACL.')
            ace = _POINTER()
            if not _security.GetAce(dacl, 0, ctypes.byref(ace)):
                raise _error()
            header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            sid = _POINTER(ace.value + 8)
            expected_flags = 3 if directory else 0
            if (header.kind != 0 or header.flags != expected_flags or mask != _FILE_ALL_ACCESS
                    or not _security.IsValidSid(sid) or not _security.EqualSid(sid, self.sid)):
                raise OSError(errno.ENOTSUP, 'The filesystem did not retain the owner-only Windows access rule.')

    def protect(self, handle, directory):
        self.validate(handle, directory)
        dacl = (self.directory if directory else self.file)[1]
        code = _security.SetSecurityInfo(handle, 1, _DACL_INFO | _PROTECTED_DACL_INFO,
                                         None, None, dacl, None)
        if code:
            raise ctypes.WinError(code)
        self.assert_private(handle, directory)

    def close(self):
        for descriptor in self._descriptors:
            _kernel.LocalFree(descriptor)
        self._descriptors.clear()
        for sid in self._owned_sids:
            _kernel.LocalFree(sid)
        self._owned_sids.clear()


class PrivateDirectory:
    def __init__(self, path):
        self._handles = []
        self._handle = None
        self._security = None
        self.path = _absolute(path)
        try:
            self._security = _Security()
            current = Path(self.path.anchor)
            anchor = _open(current, True, repair=False)
            self._handles.append(anchor)
            self._security.validate(anchor, True, owner=False)
            for component in self.path.parts[1:]:
                current /= component
                created = False
                try:
                    handle = _open(current, True, repair=current == self.path)
                except FileNotFoundError:
                    attributes = self._security.directory[0]
                    if _kernel.CreateDirectoryW(str(current), ctypes.byref(attributes)):
                        created = True
                    else:
                        error = _error()
                        if error.winerror != 183:
                            raise error
                    handle = _open(current, True, repair=current == self.path)
                self._handles.append(handle)
                self._security.validate(handle, True, owner=created or current == self.path)
                if created:
                    self._security.assert_private(handle, True)
            self._handle = self._handles[-1]
            self.identity = self._security.validate(self._handle, True)
            self._repair_tree()
            self.verify()
        except BaseException:
            self.close()
            raise

    def _repair_tree(self):
        entries = [(self._handle, True)]
        retained = []
        try:
            def inspect(path):
                with os.scandir(path) as children:
                    for child in children:
                        try:
                            _name(child.name)
                            directory = child.is_dir(follow_symlinks=False)
                            handle = _open(child.path, directory)
                        except FileNotFoundError:
                            continue
                        retained.append(handle)
                        self._security.validate(handle, directory)
                        entries.append((handle, directory))
                        if directory:
                            inspect(Path(child.path))
            inspect(self.path)
            for handle, directory in entries:
                if directory:
                    self._security.preflight_directory(handle)
            # Validate the entire legacy tree before changing any existing ACL;
            # protected children are repaired before inheritable parent rules.
            for handle, directory in reversed(entries):
                self._security.protect(handle, directory)
        finally:
            for handle in reversed(retained):
                _close(handle)

    def verify(self):
        if self._handle is None:
            raise ValueError('The private job directory is closed.')
        handle = _open(self.path, True, repair=False)
        try:
            if self._security.validate(handle, True) != self.identity:
                raise ValueError('The private job directory changed; restore its original location before retrying.')
        finally:
            _close(handle)
        self._security.assert_private(self._handle, True)

    def child(self, name):
        self.verify()
        return PrivateDirectory(self.path / _name(name))

    def open_file(self, name, *, exclusive=False):
        self.verify()
        target = self.path / _name(name)
        attributes = self._security.file[0]
        handle = _kernel.CreateFileW(str(target), 0xC0000000 | _READ_CONTROL | _WRITE_DAC,
                                     _SHARE_READ_WRITE, ctypes.byref(attributes),
                                     1 if exclusive else 4, _OPEN_REPARSE_POINT, None)
        if handle == _INVALID_HANDLE:
            raise _error()
        try:
            self._security.protect(handle, False)
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            handle = None
            try:
                return os.fdopen(descriptor, 'r+b', buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
        finally:
            _close(handle)

    def duplicate_fd(self):
        raise OSError(errno.ENOTSUP, 'Windows private directory handles are not POSIX directory descriptors.')

    def close(self):
        for handle in reversed(getattr(self, '_handles', [])):
            _close(handle)
        self._handles = []
        self._handle = None
        if getattr(self, '_security', None) is not None:
            self._security.close()
            self._security = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def __del__(self):
        self.close()
