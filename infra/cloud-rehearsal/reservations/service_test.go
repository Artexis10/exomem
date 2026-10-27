package lvmd

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	lvmdTypes "github.com/topolvm/topolvm/pkg/lvmd/types"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
)

// This boundary uses the actual provider, gRPC transport, metadata and host LVM.
// The sizes are invented fixture data aligned to the fixture's 4 MiB extents.
func TestReservationPhysicalRestart(t *testing.T) {
	if os.Geteuid() != 0 || os.Getenv("EXOMEM_RESERVATION_RUNNER") != "disposable-github-runner" {
		t.Fatal("this proof requires its disposable GitHub runner fixture")
	}
	const mib = int64(1 << 20)
	const class = "guarded"
	if os.Getenv("PROOF_SERVICE_CHILD") == "1" {
		concurrency, err := strconv.ParseUint(os.Getenv("PROOF_CONCURRENCY"), 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		dc := &lvmdTypes.DeviceClass{Name: class, Type: lvmdTypes.TypeThin, VolumeGroup: os.Getenv("PROOF_VG"), ThinPoolConfig: &lvmdTypes.ThinPoolConfig{Name: os.Getenv("PROOF_POOL"), OverprovisionRatio: 1}, ReservationPolicy: &lvmdTypes.ReservationPolicy{BackupConcurrency: concurrency, OrdinaryVolumeCapBytes: uint64(256 * mib), LockDirectory: os.Getenv("PROOF_PROVIDER_LOCKS")}}
		listener, err := net.Listen("unix", os.Getenv("PROOF_SOCKET"))
		if err != nil {
			t.Fatal(err)
		}
		server := grpc.NewServer()
		proto.RegisterLVServiceServer(server, NewLVService(NewDeviceClassManager([]*lvmdTypes.DeviceClass{dc}), NewLvcreateOptionClassManager(nil), nil))
		if err := server.Serve(listener); err != nil {
			t.Fatal(err)
		}
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	client, stop := startPhysicalProvider(t, 1)
	defer func() { stop() }()
	source, err := client.CreateLV(ctx, &proto.CreateLVRequest{DeviceClass: class, Name: "source", SizeBytes: 32 * mib})
	if err != nil {
		t.Fatal(err)
	}
	identity := source.Volume.PhysicalUuid
	if identity == "" {
		t.Fatal("created source has no physical identity")
	}
	target := &proto.ReserveLVTargetRequest{DeviceClass: class, SourceVolume: "source", PhysicalSourceUuid: identity, TargetBytes: uint64(140 * mib)}
	ack, err := client.ReserveLVTarget(ctx, target)
	if err != nil || ack.ReservedBytes != target.TargetBytes || ack.PhysicalUuid != identity {
		t.Fatalf("target acknowledgement: %v %v", ack, err)
	}
	stop()
	client, stop = startPhysicalProvider(t, 1)
	frame := physicalBudget(t, ctx, client)
	// This assertion catches an acknowledgement whose physical metadata was not committed.
	if frame.OrdinaryBytes != uint64(140*mib) {
		t.Fatalf("accepted target disappeared across provider SIGKILL: ordinary=%d want=%d", frame.OrdinaryBytes, 140*mib)
	}
	for _, allocation := range frame.Allocations {
		if allocation.Name == "source" && (allocation.SizeBytes != uint64(32*mib) || allocation.ReservedBytes != uint64(140*mib)) {
			t.Fatalf("reservation resized its source or lost the target: %v", allocation)
		}
	}
	// A physical LV outside the provider API must still consume ordinary capacity.
	rawPhysicalLVM(t, "lvcreate", "--yes", "--virtualsize", "64M", "--thinpool", os.Getenv("PROOF_VG")+"/"+os.Getenv("PROOF_POOL"), "--name", "orphan")
	frame = physicalBudget(t, ctx, client)
	if frame.PoolSizeBytes != uint64(512*mib) || frame.OrdinaryBytes != uint64(204*mib) || frame.EffectiveBackupReserveBytes != uint64(280*mib) {
		t.Fatalf("physical orphan or target budget differs: %v", frame)
	}
	var wg sync.WaitGroup
	results := make(chan codes.Code, 2)
	start := make(chan struct{})
	for _, name := range []string{"claim-one", "claim-two"} {
		wg.Add(1)
		go func(name string) {
			defer wg.Done()
			<-start
			_, err := client.CreateLV(ctx, &proto.CreateLVRequest{DeviceClass: class, Name: name, SizeBytes: 20 * mib})
			results <- status.Code(err)
		}(name)
	}
	close(start)
	wg.Wait()
	close(results)
	success, refused := 0, 0
	for code := range results {
		if code == codes.OK {
			success++
		} else if code == codes.ResourceExhausted {
			refused++
		}
	}
	if success != 1 || refused != 1 {
		t.Fatalf("physical competing claims: successful=%d refused=%d", success, refused)
	}
	for _, name := range []string{"claim-one", "claim-two"} {
		_, err := client.RemoveLV(ctx, &proto.RemoveLVRequest{DeviceClass: class, Name: name})
		if err != nil && status.Code(err) != codes.NotFound {
			t.Fatal(err)
		}
	}
	request := &proto.FamilyRequest{DeviceClass: class, SourceVolume: "source", PhysicalSourceUuid: identity, RequestId: "cancelled"}
	closed, err := client.EndBackupFamily(ctx, request)
	if err != nil || closed.State != "closed" || closed.ClosedEpoch != 1 {
		t.Fatalf("End before Begin: %v %v", closed, err)
	}
	t.Logf("family_after_early_end=%v", closed)
	_, err = client.BeginBackupFamily(ctx, request)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("Begin revived a cancelled request: %v", err)
	}
	activeRequest := &proto.FamilyRequest{DeviceClass: class, SourceVolume: "source", PhysicalSourceUuid: identity, ExpectedClosedEpoch: 1, RequestId: "backup"}
	active, err := client.BeginBackupFamily(ctx, activeRequest)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("family_after_begin=%v", active)
	snapshot, err := client.CreateLVSnapshot(ctx, &proto.CreateLVSnapshotRequest{DeviceClass: class, SourceVolume: "source", ImmediateSourceUuid: identity, FamilyToken: active.Token, Name: "backup-child", AccessType: "ro", SizeBytes: 32 * mib})
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.CreateLVSnapshot(ctx, &proto.CreateLVSnapshotRequest{DeviceClass: class, SourceVolume: "backup-child", ImmediateSourceUuid: snapshot.Snapshot.PhysicalUuid, FamilyToken: active.Token, Name: "clone", AccessType: "rw", SizeBytes: 32 * mib})
	if err != nil {
		t.Fatal(err)
	}
	// A real open block device prevents lvremove after End commits revocation.
	openSnapshot, err := os.Open(snapshot.Snapshot.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer openSnapshot.Close()
	_, err = client.EndBackupFamily(ctx, activeRequest)
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("open snapshot did not interrupt family cleanup: %v", err)
	}
	stop()
	client, stop = startPhysicalProvider(t, 0)
	current, err := client.GetBackupFamily(ctx, activeRequest)
	if err != nil || current.State != "retiring" {
		t.Fatalf("retiring state lost across restart: %v %v", current, err)
	}
	t.Logf("family_after_restart=%v", current)
	frame = physicalBudget(t, ctx, client)
	if frame.OutstandingFamilyReservationBytes != uint64(64*mib) || frame.EffectiveBackupReserveBytes != uint64(64*mib) {
		t.Fatalf("concurrency reduction released retiring charge: %v", frame)
	}
	_, err = client.CreateLVSnapshot(ctx, &proto.CreateLVSnapshotRequest{DeviceClass: class, SourceVolume: "source", ImmediateSourceUuid: identity, FamilyToken: active.Token, Name: "late", AccessType: "ro", SizeBytes: 32 * mib})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("retiring token allocated a child: %v", err)
	}
	_, err = client.BeginBackupFamily(ctx, &proto.FamilyRequest{DeviceClass: class, SourceVolume: "orphan", PhysicalSourceUuid: allocationUUID(t, frame, "orphan"), RequestId: "disabled"})
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("zero concurrency admitted a new family: %v", err)
	}
	if err := openSnapshot.Close(); err != nil {
		t.Fatal(err)
	}
	closed, err = client.EndBackupFamily(ctx, activeRequest)
	if err != nil || closed.State != "closed" {
		t.Fatalf("physical cleanup retry: %v %v", closed, err)
	}
	t.Logf("family_after_cleanup=%v", closed)
	if frame = physicalBudget(t, ctx, client); frame.OutstandingFamilyReservationBytes != 0 {
		t.Fatalf("physical child absence did not release charge: %v", frame)
	}
	stop()
	client, stop = startPhysicalProvider(t, 1)
	nextRequest := &proto.FamilyRequest{DeviceClass: class, SourceVolume: "source", PhysicalSourceUuid: identity, ExpectedClosedEpoch: 2, RequestId: "next-backup"}
	next, err := client.BeginBackupFamily(ctx, nextRequest)
	if err != nil || next.Token == active.Token {
		t.Fatalf("new family identity: %v %v", next, err)
	}
	_, err = client.CreateLVSnapshot(ctx, &proto.CreateLVSnapshotRequest{DeviceClass: class, SourceVolume: "source", ImmediateSourceUuid: identity, FamilyToken: active.Token, Name: "stale", AccessType: "ro", SizeBytes: 32 * mib})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("old token allocated after new Begin: %v", err)
	}
	_, err = client.EndBackupFamily(ctx, request)
	if err != nil {
		t.Fatal(err)
	}
	current, err = client.GetBackupFamily(ctx, nextRequest)
	if err != nil || current.Token != next.Token || current.State != "active" {
		t.Fatalf("old End closed a new family: %v %v", current, err)
	}
	if _, err = client.EndBackupFamily(ctx, nextRequest); err != nil {
		t.Fatal(err)
	}
	if _, err = client.RemoveLV(ctx, &proto.RemoveLVRequest{DeviceClass: class, Name: "source"}); err != nil {
		t.Fatal(err)
	}
	recreated, err := client.CreateLV(ctx, &proto.CreateLVRequest{DeviceClass: class, Name: "source", SizeBytes: 32 * mib})
	if err != nil || recreated.Volume.PhysicalUuid == identity {
		t.Fatalf("physical source recreation: %v %v", recreated, err)
	}
	_, err = client.EndBackupFamily(ctx, nextRequest)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("old physical identity matched a recreated source: %v", err)
	}
	t.Logf("physical_UUID_before=%s physical_UUID_after=%s old_epoch=%d new_epoch=%d", identity, recreated.Volume.PhysicalUuid, active.Epoch, next.Epoch)
	physicalBudget(t, ctx, client)
}

func startPhysicalProvider(t *testing.T, concurrency uint64) (proto.LVServiceClient, func()) {
	t.Helper()
	file, err := os.CreateTemp(os.Getenv("PROOF_EVIDENCE"), "service-*.log")
	if err != nil {
		t.Fatal(err)
	}
	if err := file.Chmod(0644); err != nil {
		_ = file.Close()
		t.Fatal(err)
	}
	socket := filepath.Join(os.Getenv("PROOF_ROOT"), fmt.Sprintf("service-%d.sock", time.Now().UnixNano()))
	process := exec.Command(os.Args[0], "-test.run=^TestReservationPhysicalRestart$", "-test.v")
	process.Env = append(os.Environ(), "PROOF_SERVICE_CHILD=1", "PROOF_SOCKET="+socket, fmt.Sprintf("PROOF_CONCURRENCY=%d", concurrency))
	process.Stdout, process.Stderr = file, file
	if err := process.Start(); err != nil {
		t.Fatal(err)
	}
	connection, err := grpc.NewClient("unix://"+socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		_ = process.Process.Kill()
		_ = process.Wait()
		t.Fatal(err)
	}
	var once sync.Once
	stop := func() {
		once.Do(func() {
			_ = connection.Close()
			_ = process.Process.Kill()
			_ = process.Wait()
			_ = file.Close()
			_ = os.Remove(socket)
		})
	}
	t.Cleanup(stop)
	client := proto.NewLVServiceClient(connection)
	deadline := time.NewTimer(15 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(20 * time.Millisecond)
	defer tick.Stop()
	for {
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		_, err := client.GetPoolBudget(ctx, &proto.GetPoolBudgetRequest{DeviceClass: "guarded"})
		cancel()
		if err == nil {
			return client, stop
		}
		select {
		case <-deadline.C:
			t.Fatalf("physical provider did not become ready: %v", err)
		case <-tick.C:
		}
	}
}

func physicalBudget(t *testing.T, ctx context.Context, client proto.LVServiceClient) *proto.GetPoolBudgetResponse {
	t.Helper()
	frame, err := client.GetPoolBudget(ctx, &proto.GetPoolBudgetRequest{DeviceClass: "guarded"})
	if err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(frame)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("physical_budget=%s", data)
	return frame
}

func allocationUUID(t *testing.T, frame *proto.GetPoolBudgetResponse, name string) string {
	t.Helper()
	for _, allocation := range frame.Allocations {
		if allocation.Name == name {
			return allocation.PhysicalUuid
		}
	}
	t.Fatalf("physical allocation absent: %s", name)
	return ""
}

func rawPhysicalLVM(t *testing.T, args ...string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, "/sbin/lvm", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("fixture LVM %v: %v %s", args, err, output)
	}
}
