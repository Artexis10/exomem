#define _GNU_SOURCE
#include <Python.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/magic.h>
#include <linux/openat2.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/vfs.h>
#include <unistd.h>

/* Fixed Linux openat2 and held-filesystem protocol, never application policy. */
#define DIRECTORY_FLAGS (O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
#define READ_FLAGS (O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
#define IDENTITY_FLAGS (O_PATH | O_NOFOLLOW | O_CLOEXEC)
#define RESOLUTION_FLAGS (RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV)
#define CAPSULE_NAME "exomem.native.version_probe"

typedef struct {
    int root, parent, leaf, grandparent;
    PyObject *parent_path, *leaf_name;
    struct stat root_info, parent_info, version;
} Probe;

static int checked(PyObject *check) {
    if (PyErr_CheckSignals() < 0) return -1;
    if (check == Py_None) return 0;
    PyObject *result = PyObject_CallNoArgs(check);
    if (result == NULL) return -1;
    Py_DECREF(result);
    return 0;
}

static int relative_open(int parent, const char *name, int flags) {
    struct open_how how = {.flags = (uint64_t)flags, .resolve = RESOLUTION_FLAGS};
    int descriptor, failure;
    Py_BEGIN_ALLOW_THREADS
    descriptor = (int)syscall(SYS_openat2, parent, name, &how, sizeof(how));
    failure = errno;
    Py_END_ALLOW_THREADS
    errno = failure;
    return descriptor;
}

static int descriptor_stat(int descriptor, struct stat *info) {
    int result, failure;
    Py_BEGIN_ALLOW_THREADS
    result = fstat(descriptor, info);
    failure = errno;
    Py_END_ALLOW_THREADS
    errno = failure;
    return result;
}

static int same_identity(const struct stat *first, const struct stat *second) {
    return first->st_dev == second->st_dev && first->st_ino == second->st_ino
        && (first->st_mode & S_IFMT) == (second->st_mode & S_IFMT);
}

static int same_version(const struct stat *first, const struct stat *second) {
    return same_identity(first, second) && first->st_nlink == second->st_nlink
        && first->st_size == second->st_size
        && first->st_mtim.tv_sec == second->st_mtim.tv_sec
        && first->st_mtim.tv_nsec == second->st_mtim.tv_nsec
        && first->st_ctim.tv_sec == second->st_ctim.tv_sec
        && first->st_ctim.tv_nsec == second->st_ctim.tv_nsec;
}

static PyObject *identity(const struct stat *info) {
    /* StableIdentity.kind is the closed set of filesystem object kinds. */
    const char *kind = S_ISDIR(info->st_mode) ? "directory" : S_ISREG(info->st_mode) ? "file" : "other";
    return Py_BuildValue("(KKsK)", (unsigned long long)info->st_dev,
                         (unsigned long long)info->st_ino, kind, (unsigned long long)info->st_nlink);
}

static int admit(PyObject *callback, const struct stat *info) {
    PyObject *facts = identity(info);
    if (facts == NULL) return -1;
    PyObject *result = PyObject_CallOneArg(callback, facts);
    Py_DECREF(facts);
    if (result == NULL) return -1;
    Py_DECREF(result);
    return 0;
}

static PyObject *nanoseconds(struct timespec value) {
    PyObject *seconds = PyLong_FromLongLong((long long)value.tv_sec);
    PyObject *scale = PyLong_FromLong(1000000000);
    PyObject *fraction = PyLong_FromLong(value.tv_nsec);
    PyObject *whole = NULL, *result = NULL;
    if (seconds != NULL && scale != NULL && fraction != NULL) {
        whole = PyNumber_Multiply(seconds, scale);
        if (whole != NULL) result = PyNumber_Add(whole, fraction);
    }
    Py_XDECREF(seconds); Py_XDECREF(scale); Py_XDECREF(fraction); Py_XDECREF(whole);
    return result;
}

static PyObject *version(const struct stat *info) {
    PyObject *facts = identity(info);
    PyObject *size = PyLong_FromLongLong((long long)info->st_size);
    PyObject *mtime = nanoseconds(info->st_mtim), *ctime = nanoseconds(info->st_ctim);
    PyObject *result = NULL;
    if (facts != NULL && size != NULL && mtime != NULL && ctime != NULL)
        result = PyTuple_Pack(4, facts, size, mtime, ctime);
    Py_XDECREF(facts); Py_XDECREF(size); Py_XDECREF(mtime); Py_XDECREF(ctime);
    return result;
}

static int close_descriptor(int *descriptor) {
    int held = *descriptor;
    *descriptor = -1;
    return held >= 0 ? close(held) : 0;
}

static int close_probe(Probe *probe) {
    int failure = 0;
    Py_BEGIN_ALLOW_THREADS
    if (probe->grandparent == probe->root) probe->grandparent = -1;
    if (probe->parent == probe->root) probe->parent = -1;
    if (close_descriptor(&probe->grandparent) < 0) failure = errno;
    if (close_descriptor(&probe->leaf) < 0 && failure == 0) failure = errno;
    if (close_descriptor(&probe->parent) < 0 && failure == 0) failure = errno;
    if (close_descriptor(&probe->root) < 0 && failure == 0) failure = errno;
    Py_END_ALLOW_THREADS
    return failure;
}

static void destroy_probe(PyObject *capsule) {
    Probe *probe = PyCapsule_GetPointer(capsule, CAPSULE_NAME);
    if (probe == NULL) return;
    close_probe(probe);
    Py_XDECREF(probe->parent_path); Py_XDECREF(probe->leaf_name);
    PyMem_Free(probe);
}

static const char *path_bytes(PyObject *value) {
    if (!PyBytes_CheckExact(value)) {
        PyErr_SetString(PyExc_TypeError, "physical path must be bytes");
        return NULL;
    }
    const char *text = PyBytes_AS_STRING(value);
    if (PyBytes_GET_SIZE(value) == 0 || memchr(text, 0, (size_t)PyBytes_GET_SIZE(value))) {
        errno = EINVAL;
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    return text;
}

static int component(const char *name, size_t length) {
    return length > 0 && !(length == 1 && name[0] == '.')
        && !(length == 2 && name[0] == '.' && name[1] == '.')
        && memchr(name, '\\', length) == NULL;
}

static PyObject *open_version(PyObject *module, PyObject *args) {
    (void)module;
    PyObject *root_path, *parent_path, *leaf_name, *callback, *check;
    if (!PyArg_ParseTuple(args, "OOOOO", &root_path, &parent_path, &leaf_name, &callback, &check)) return NULL;
    const char *root = path_bytes(root_path);
    if (root == NULL) return NULL;
    const char *parent = path_bytes(parent_path);
    if (parent == NULL) return NULL;
    const char *leaf = path_bytes(leaf_name);
    if (leaf == NULL) return NULL;
    if (!component(leaf, strlen(leaf)) || strchr(leaf, '/')) {
        errno = EINVAL; return PyErr_SetFromErrno(PyExc_OSError);
    }
    Probe *probe = PyMem_Calloc(1, sizeof(*probe));
    if (probe == NULL) return PyErr_NoMemory();
    probe->root = probe->parent = probe->leaf = probe->grandparent = -1;
    probe->parent_path = Py_NewRef(parent_path); probe->leaf_name = Py_NewRef(leaf_name);
    PyObject *capsule = PyCapsule_New(probe, CAPSULE_NAME, destroy_probe);
    if (capsule == NULL) {
        Py_DECREF(parent_path); Py_DECREF(leaf_name); PyMem_Free(probe); return NULL;
    }
    PyObject *result = NULL, *root_facts = NULL, *parent_facts = NULL, *file_version = NULL;
    struct stat grandparent_info, observed, opened_leaf;
    char name[NAME_MAX + 1] = {0};
    int failure = 0;
    if (checked(check) < 0) goto done;
    Py_BEGIN_ALLOW_THREADS
    probe->root = open(root, DIRECTORY_FLAGS);
    failure = errno;
    Py_END_ALLOW_THREADS
    if (probe->root < 0) { errno = failure; goto io_error; }
    if (descriptor_stat(probe->root, &probe->root_info) < 0) goto io_error;
    if (!S_ISDIR(probe->root_info.st_mode)) { errno = ELOOP; goto io_error; }
    probe->parent = probe->root;
    if (strcmp(parent, ".") != 0) {
        const char *start = parent;
        while (*start) {
            const char *slash = strchr(start, '/');
            size_t length = slash ? (size_t)(slash - start) : strlen(start);
            if (!component(start, length) || length > NAME_MAX || (slash && slash[1] == 0)) {
                errno = length > NAME_MAX ? ENAMETOOLONG : EINVAL; goto io_error;
            }
            memcpy(name, start, length); name[length] = 0;
            if (checked(check) < 0) goto done;
            int child = relative_open(probe->parent, name, DIRECTORY_FLAGS);
            if (child < 0) goto io_error;
            int previous = probe->parent;
            probe->parent = child;
            if (slash == NULL) probe->grandparent = previous;
            else if (previous != probe->root) {
                if (close(previous) < 0) goto io_error;
            }
            if (slash == NULL) break;
            start = slash + 1;
        }
    }
    if (descriptor_stat(probe->parent, &probe->parent_info) < 0) goto io_error;
    if (!S_ISDIR(probe->parent_info.st_mode)) { errno = ELOOP; goto io_error; }
    if (probe->grandparent >= 0 && descriptor_stat(probe->grandparent, &grandparent_info) < 0) goto io_error;
    /* This cut must run before any ordinary leaf descriptor can release record locks. */
    if (admit(callback, &probe->parent_info) < 0 || checked(check) < 0) goto done;
    probe->leaf = relative_open(probe->parent, leaf, READ_FLAGS);
    if (probe->leaf < 0 || descriptor_stat(probe->leaf, &opened_leaf) < 0) goto io_error;
    if (!S_ISREG(opened_leaf.st_mode)) { errno = ELOOP; goto io_error; }
    if (admit(callback, &opened_leaf) < 0) goto done;
    if (descriptor_stat(probe->parent, &observed) < 0) goto io_error;
    if (!same_identity(&observed, &probe->parent_info)) { errno = ESTALE; goto io_error; }
    if (probe->grandparent >= 0) {
        if (descriptor_stat(probe->grandparent, &observed) < 0) goto io_error;
        if (!same_identity(&observed, &grandparent_info)) { errno = ESTALE; goto io_error; }
        int status;
        Py_BEGIN_ALLOW_THREADS
        status = fstatat(probe->grandparent, name, &observed, AT_SYMLINK_NOFOLLOW);
        failure = errno;
        Py_END_ALLOW_THREADS
        if (status < 0) { errno = failure; goto io_error; }
        if (!same_identity(&observed, &probe->parent_info)) { errno = ESTALE; goto io_error; }
    }
    if (descriptor_stat(probe->leaf, &probe->version) < 0) goto io_error;
    if (!same_identity(&probe->version, &opened_leaf) || probe->version.st_nlink != opened_leaf.st_nlink) {
        errno = ESTALE; goto io_error;
    }
    struct statfs filesystem;
    int supported;
    Py_BEGIN_ALLOW_THREADS
    supported = fstatfs(probe->leaf, &filesystem);
    failure = errno;
    Py_END_ALLOW_THREADS
    if (supported < 0) { errno = failure; goto io_error; }
    /* Kernel-defined ext2/ext3/ext4 magic, descriptor-bound through native headers. */
    if (filesystem.f_type != EXT2_SUPER_MAGIC) { errno = EOPNOTSUPP; goto io_error; }
    if (probe->grandparent == probe->root) probe->grandparent = -1;
    if (close_descriptor(&probe->grandparent) < 0) goto io_error;
    if (checked(check) < 0) goto done;
    root_facts = identity(&probe->root_info); parent_facts = identity(&probe->parent_info);
    file_version = version(&probe->version);
    if (root_facts != NULL && parent_facts != NULL && file_version != NULL)
        result = PyTuple_Pack(4, capsule, root_facts, parent_facts, file_version);
    goto done;
io_error:
    PyErr_SetFromErrno(PyExc_OSError);
done:
    Py_XDECREF(root_facts); Py_XDECREF(parent_facts); Py_XDECREF(file_version); Py_DECREF(capsule);
    return result;
}

static PyObject *require_current(PyObject *module, PyObject *args) {
    (void)module;
    PyObject *capsule, *callback, *check;
    if (!PyArg_ParseTuple(args, "OOO", &capsule, &callback, &check)) return NULL;
    Probe *probe = PyCapsule_GetPointer(capsule, CAPSULE_NAME);
    if (probe == NULL) return NULL;
    int parent = -1, leaf = -1, failure = 0;
    struct stat current_parent, current_leaf;
    PyObject *result = NULL;
    if (probe->root < 0) { errno = ESTALE; goto io_error; }
    if (checked(check) < 0) goto done;
    parent = relative_open(probe->root, PyBytes_AS_STRING(probe->parent_path), IDENTITY_FLAGS | O_DIRECTORY);
    if (parent < 0 || descriptor_stat(parent, &current_parent) < 0) goto io_error;
    /* Closing admission precedes comparison and every subsequent child acquisition. */
    if (admit(callback, &current_parent) < 0) goto done;
    if (!same_identity(&current_parent, &probe->parent_info)) { errno = ESTALE; goto io_error; }
    if (checked(check) < 0) goto done;
    leaf = relative_open(parent, PyBytes_AS_STRING(probe->leaf_name), IDENTITY_FLAGS);
    if (leaf < 0 || descriptor_stat(leaf, &current_leaf) < 0) goto io_error;
    if (!S_ISREG(current_leaf.st_mode)) { errno = ELOOP; goto io_error; }
    if (admit(callback, &current_leaf) < 0) goto done;
    if (!same_version(&current_leaf, &probe->version)) { errno = ESTALE; goto io_error; }
    if (checked(check) < 0) goto done;
    result = Py_NewRef(Py_None);
    goto done;
io_error:
    PyErr_SetFromErrno(PyExc_OSError);
done:
    Py_BEGIN_ALLOW_THREADS
    if (close_descriptor(&leaf) < 0) failure = errno;
    if (close_descriptor(&parent) < 0 && failure == 0) failure = errno;
    Py_END_ALLOW_THREADS
    if (failure && result != NULL) {
        Py_CLEAR(result); errno = failure; PyErr_SetFromErrno(PyExc_OSError);
    }
    return result;
}

static PyObject *close_operation(PyObject *module, PyObject *capsule) {
    (void)module;
    Probe *probe = PyCapsule_GetPointer(capsule, CAPSULE_NAME);
    if (probe == NULL) return NULL;
    int failure = close_probe(probe);
    if (failure) { errno = failure; return PyErr_SetFromErrno(PyExc_OSError); }
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"open_version", open_version, METH_VARARGS, "Observe a fresh ordinary leaf through its parent admission cut."},
    {"require_current", require_current, METH_VARARGS, "Rebind through a fresh closing admission cut."},
    {"close", close_operation, METH_O, "Close every descriptor owned by one observation."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_held_fs_linux_native",
    .m_doc = "Private bounded Linux file observation prototype.",
    .m_size = -1,
    .m_methods = methods,
};

PyMODINIT_FUNC PyInit__held_fs_linux_native(void) {
    PyObject *result = PyModule_Create(&module);
    if (result == NULL) return NULL;
    /* Fixed descriptor state, component buffer, facts and C-API construction overlap;
       path encodings are admitted separately by the Python resource owner. */
    if (PyModule_AddIntConstant(result, "WORKSPACE_BYTES", 8192 + sizeof(Probe) + 2 * sizeof(struct stat) + NAME_MAX + 1) < 0) {
        Py_DECREF(result); return NULL;
    }
    return result;
}
