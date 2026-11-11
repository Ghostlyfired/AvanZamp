"""Nonblocking process lock supported by both Windows and POSIX."""
import os


def lock_file(handle):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(' ')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError('Workspace is already locked.') from exc
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
