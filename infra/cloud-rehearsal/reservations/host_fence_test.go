package command

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// This test belongs in the assembled command package, never the upstream checkout.
// The prefix is the closed process contract that this proof must exercise unchanged.
func TestReservationHostLVMParentDeath(t *testing.T) {
	if os.Geteuid() != 0 || os.Getenv("EXOMEM_RESERVATION_RUNNER") != "disposable-github-runner" {
		t.Fatal("this proof requires its disposable GitHub runner fixture")
	}
	vg, pool := os.Getenv("PROOF_VG"), os.Getenv("PROOF_POOL")
	directory, tag := os.Getenv("PROOF_PROVIDER_LOCKS"), os.Getenv("PROOF_FENCE_TAG")
	if os.Getenv("PROOF_FENCE_CHILD") == "1" {
		if err := WithPoolTransaction(context.Background(), directory, vg+"/"+pool, func(ctx context.Context) error {
			return callLVM(ctx, "lvchange", "--quiet", "--addtag", tag, vg+"/fence")
		}); err != nil {
			t.Fatal(err)
		}
		return
	}
	wantPrefix := []string{"/usr/bin/nsenter", "-m", "-u", "-i", "-n", "-p", "-t", "1", "/sbin/lvm"}
	if !reflect.DeepEqual(lvmCommandPrefix, wantPrefix) {
		t.Fatalf("default process chain changed: %q", lvmCommandPrefix)
	}
	binary := os.Getenv("PROOF_LVM_BINARY")
	if binary == "" {
		binary = "/sbin/lvm"
	}
	wanted, err := os.Stat(binary)
	if err != nil {
		t.Fatalf("host LVM identity path is unavailable: %s: %v", binary, err)
	}
	// Adopt the orphan for bounded cleanup; this does not alter either command.
	if err := unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0); err != nil {
		t.Fatal(err)
	}
	nativePath := filepath.Join(os.Getenv("PROOF_NATIVE_LOCKS"), "V_"+vg)
	native, err := os.OpenFile(nativePath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer native.Close()
	if err := unix.Flock(int(native.Fd()), unix.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	defer unix.Flock(int(native.Fd()), unix.LOCK_UN)
	nativeInfo, err := native.Stat()
	if err != nil {
		t.Fatal(err)
	}
	logFile, err := os.Create(filepath.Join(os.Getenv("PROOF_EVIDENCE"), "fence-parent.log"))
	if err != nil {
		t.Fatal(err)
	}
	defer logFile.Close()
	parent := exec.Command(os.Args[0], "-test.run=^TestReservationHostLVMParentDeath$", "-test.v")
	parent.Env = append(os.Environ(), "PROOF_FENCE_CHILD=1")
	parent.Stdout, parent.Stderr = logFile, logFile
	if err := parent.Start(); err != nil {
		t.Fatal(err)
	}
	parentWaited := false
	child := 0
	defer func() {
		if !parentWaited {
			_ = parent.Process.Kill()
			_ = parent.Wait()
		}
		if child != 0 {
			_ = unix.Kill(child, unix.SIGKILL)
			_, _ = unix.Wait4(child, nil, 0, nil)
		}
	}()
	waitHostCondition(t, "real host LVM blocked on the native VG lock", func() bool {
		entries, _ := os.ReadDir("/proc")
		for _, entry := range entries {
			pid, err := strconv.Atoi(entry.Name())
			if err != nil {
				continue
			}
			args, _ := os.ReadFile(filepath.Join("/proc", entry.Name(), "cmdline"))
			if !strings.Contains(string(args), "\x00"+tag+"\x00") {
				continue
			}
			actual, err := os.Stat(filepath.Join("/proc", entry.Name(), "exe"))
			if err == nil && os.SameFile(actual, wanted) && hostBlockedOn(pid, nativeInfo) {
				child = pid
				return true
			}
		}
		return false
	})
	lockPath := filepath.Join(directory, fmt.Sprintf("%x.lock", sha256.Sum256([]byte(vg+"/"+pool))))
	lockInfo, err := os.Stat(lockPath)
	if err != nil {
		t.Fatal(err)
	}
	stdin, err := os.Stat(fmt.Sprintf("/proc/%d/fd/0", child))
	if err != nil || !os.SameFile(stdin, lockInfo) {
		t.Fatalf("host LVM fd0 does not retain provider lock inode: %v", err)
	}
	locks, err := os.ReadFile("/proc/locks")
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("host_lvm_binary=%s host_lvm_pid=%d parent_pid=%d fd0=%s provider_lock=%v native_lock=%v\n%s", binary, child, parent.Process.Pid, lockPath, lockInfo.Sys(), nativeInfo.Sys(), locks)
	if err := parent.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = parent.Wait()
	parentWaited = true
	if !hostBlockedOn(child, nativeInfo) {
		t.Fatal("host LVM did not survive parent SIGKILL while blocked on its native lock")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	entered := false
	err = WithPoolTransaction(ctx, directory, vg+"/"+pool, func(context.Context) error {
		entered = true
		return nil
	})
	if entered || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("competing provider transaction entered before orphan LVM exit: entered=%v error=%v", entered, err)
	}
	if !hostBlockedOn(child, nativeInfo) {
		t.Fatal("host LVM exited during the competing transaction")
	}
	if err := unix.Flock(int(native.Fd()), unix.LOCK_UN); err != nil {
		t.Fatal(err)
	}
	waitHostCondition(t, "orphan host LVM exit", func() bool {
		var state unix.WaitStatus
		pid, err := unix.Wait4(child, &state, unix.WNOHANG, nil)
		if pid == child {
			child = 0
			return true
		}
		// nsenter may reap its LVM child itself.
		return err == unix.ECHILD && !hostProcessExists(child)
	})
	child = 0
	ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := WithPoolTransaction(ctx, directory, vg+"/"+pool, func(ctx context.Context) error {
		volumeGroup, err := FindVolumeGroup(ctx, vg)
		if err != nil {
			return err
		}
		volume, err := volumeGroup.FindVolume(ctx, "fence")
		if err != nil {
			return err
		}
		for _, actual := range volume.Tags() {
			if actual == tag {
				return nil
			}
		}
		return fmt.Errorf("orphan host LVM did not commit its physical mutation")
	}); err != nil {
		t.Fatal(err)
	}
	t.Log("parent SIGKILL preserved the fence; native unlock permitted the physical mutation and next provider transaction")
}

func hostProcessExists(pid int) bool {
	_, err := os.Stat(fmt.Sprintf("/proc/%d", pid))
	return err == nil
}

func hostBlockedOn(pid int, info os.FileInfo) bool {
	locks, err := os.ReadFile("/proc/locks")
	if err != nil {
		return false
	}
	stat := info.Sys().(*syscall.Stat_t)
	for _, line := range strings.Split(string(locks), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 9 || fields[1] != "->" || fields[2] != "FLOCK" || fields[5] != strconv.Itoa(pid) {
			continue
		}
		// /proc/locks is a fixed Linux kernel interface, not a language parser.
		identity := strings.Split(fields[6], ":")
		if len(identity) != 3 {
			continue
		}
		major, _ := strconv.ParseUint(identity[0], 16, 32)
		minor, _ := strconv.ParseUint(identity[1], 16, 32)
		inode, _ := strconv.ParseUint(identity[2], 10, 64)
		if uint32(major) == unix.Major(uint64(stat.Dev)) && uint32(minor) == unix.Minor(uint64(stat.Dev)) && inode == stat.Ino {
			return true
		}
	}
	return false
}

func waitHostCondition(t *testing.T, description string, ready func() bool) {
	t.Helper()
	deadline := time.NewTimer(15 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(10 * time.Millisecond)
	defer tick.Stop()
	for {
		if ready() {
			return
		}
		select {
		case <-deadline.C:
			t.Fatalf("condition did not become true: %s", description)
		case <-tick.C:
		}
	}
}
