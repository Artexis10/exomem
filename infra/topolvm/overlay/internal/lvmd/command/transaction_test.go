package command

import (
	"bufio"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestTransactionFreshOpenSerializesGoroutines(t *testing.T) {
	directory := t.TempDir()
	var active, maxActive atomic.Int32
	var wg sync.WaitGroup
	for range 4 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			err := WithPoolTransaction(context.Background(), directory, "vg/pool", func(context.Context) error {
				n := active.Add(1)
				for old := maxActive.Load(); n > old; old = maxActive.Load() {
					if maxActive.CompareAndSwap(old, n) {
						break
					}
				}
				time.Sleep(10 * time.Millisecond)
				active.Add(-1)
				return nil
			})
			if err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if maxActive.Load() != 1 {
		t.Fatalf("transactions overlapped: %d", maxActive.Load())
	}
}

func TestTransactionDescriptorSurvivesParentClose(t *testing.T) {
	// This proves OS descriptor handoff only. It does not qualify nsenter or host LVM.
	directory := t.TempDir()
	release := filepath.Join(directory, "release")
	t.Setenv("TOPOLVM_FENCE_HELPER", release)
	old := append([]string{}, lvmCommandPrefix...)
	defer SetLVMCommandPrefix(old)
	SetLVMCommandPrefix([]string{os.Args[0], "-test.run=^TestTransactionFDHelper$", "--"})
	var output io.ReadCloser
	err := WithPoolTransaction(context.Background(), directory, "vg/pool", func(ctx context.Context) error {
		var err error
		output, err = callLVMStreamed(ctx, verbosityLVMStateNoUpdate)
		if err != nil {
			return err
		}
		line, err := bufio.NewReader(output).ReadString('\n')
		if err != nil {
			return err
		}
		if line != "locked\n" {
			t.Fatalf("child stdin did not inherit the locked description: %q", line)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.WriteFile(release, nil, 0600); _ = output.Close() }()
	ctx, cancel := context.WithTimeout(context.Background(), 80*time.Millisecond)
	defer cancel()
	if err := WithPoolTransaction(ctx, directory, "vg/pool", func(context.Context) error { return nil }); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("child failed to retain lock: %v", err)
	}
	if err := os.WriteFile(release, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := output.Close(); err != nil {
		t.Fatal(err)
	}
	ctx2, cancel2 := context.WithTimeout(context.Background(), time.Second)
	defer cancel2()
	if err := WithPoolTransaction(ctx2, directory, "vg/pool", func(context.Context) error { return nil }); err != nil {
		t.Fatalf("child exit did not release lock: %v", err)
	}
}

func TestTransactionFDHelper(t *testing.T) {
	release := os.Getenv("TOPOLVM_FENCE_HELPER")
	if release == "" {
		return
	}
	info, err := os.Stdin.Stat()
	if err != nil || !info.Mode().IsRegular() {
		os.Exit(2)
	}
	if err := unix.Flock(int(os.Stdin.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		os.Exit(3)
	}
	_, _ = os.Stdout.WriteString("locked\n")
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(release); err == nil {
			os.Exit(0)
		}
		time.Sleep(5 * time.Millisecond)
	}
	os.Exit(4)
}
