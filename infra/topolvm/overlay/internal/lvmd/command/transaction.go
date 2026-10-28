package command

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/sys/unix"
)

type transactionKey struct{}

// WithPoolTransaction opens a separate description for every transaction.
// Close, never LOCK_UN: surviving children must retain this description's flock.
func WithPoolTransaction(ctx context.Context, directory, pool string, run func(context.Context) error) error {
	if ctx.Value(transactionKey{}) != nil {
		return fmt.Errorf("nested pool transaction")
	}
	if !filepath.IsAbs(directory) {
		return fmt.Errorf("transaction lock directory must be absolute")
	}
	name := fmt.Sprintf("%x.lock", sha256.Sum256([]byte(pool)))
	fd, err := unix.Open(filepath.Join(directory, name), unix.O_CREAT|unix.O_RDWR|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0600)
	if err != nil {
		return err
	}
	file := os.NewFile(uintptr(fd), name)
	defer func() { _ = file.Close() }()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("transaction lock is not a regular file")
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		err := unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB)
		if err == nil {
			break
		}
		if err != unix.EWOULDBLOCK && err != unix.EINTR {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(10 * time.Millisecond):
		}
	}
	return run(context.WithValue(ctx, transactionKey{}, file))
}

// ReplaceTag makes one metadata commit, so readers never observe half a record.
func (l *LogicalVolume) ReplaceTag(ctx context.Context, old, next string) error {
	if old == next {
		return nil
	}
	args := []string{"lvchange"}
	if old != "" {
		args = append(args, "--deltag", old)
	}
	args = append(args, "--addtag", next, l.FullName())
	return callLVM(ctx, args...)
}

// UUID identifies this physical incarnation independently of its reusable name.
func (l *LogicalVolume) UUID() string { return l.uuid }

// ExtentSize is LVM's allocation quantum, observed from the volume group.
func (vg *VolumeGroup) ExtentSize() uint64 { return vg.state.extentSize }
