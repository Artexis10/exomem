package lvmd

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	lvmdTypes "github.com/topolvm/topolvm/pkg/lvmd/types"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	gproto "google.golang.org/protobuf/proto"
)

type fakeReservationLVM struct {
	state    physicalInventory
	sequence int
	fail     string
}

func (b *fakeReservationLVM) inventory(context.Context) (*physicalInventory, error) {
	i := &physicalInventory{total: b.state.total, quantum: b.state.quantum, volumes: map[string]*proto.LogicalVolume{}}
	for n, v := range b.state.volumes {
		i.volumes[n] = gproto.Clone(v).(*proto.LogicalVolume)
	}
	return i, nil
}
func (b *fakeReservationLVM) create(_ context.Context, n string, s uint64, tags []string) error {
	b.sequence++
	b.state.volumes[n] = &proto.LogicalVolume{Name: n, PhysicalUuid: fmt.Sprintf("physical-%d", b.sequence), SizeBytes: int64(s), Pool: "pool", Tags: append([]string{}, tags...)}
	return b.failure("create")
}
func (b *fakeReservationLVM) snapshot(ctx context.Context, source, n string, tags []string) error {
	return b.create(ctx, n, uint64(b.state.volumes[source].SizeBytes), tags)
}
func (b *fakeReservationLVM) resize(_ context.Context, n string, s uint64) error {
	if err := b.failure("resize"); err != nil {
		return err
	}
	b.state.volumes[n].SizeBytes = int64(s)
	return nil
}
func (b *fakeReservationLVM) activate(context.Context, string, string) error {
	return b.failure("activate")
}
func (b *fakeReservationLVM) replaceTag(_ context.Context, n, old, next string) error {
	v := b.state.volumes[n]
	tags := []string{}
	for _, t := range v.Tags {
		if t != old {
			tags = append(tags, t)
		}
	}
	v.Tags = append(tags, next)
	return b.failure("metadata")
}
func (b *fakeReservationLVM) remove(_ context.Context, n string) error {
	if err := b.failure("remove"); err != nil {
		return err
	}
	delete(b.state.volumes, n)
	return nil
}
func (b *fakeReservationLVM) failure(op string) error {
	if b.fail == op {
		b.fail = ""
		return fmt.Errorf("injected %s interruption", op)
	}
	return nil
}
func reservationFixture(t *testing.T, total, concurrency uint64) (*lvService, *fakeReservationLVM) {
	t.Helper()
	b := &fakeReservationLVM{state: physicalInventory{total: total, quantum: 1, volumes: map[string]*proto.LogicalVolume{}}}
	dc := &lvmdTypes.DeviceClass{Name: "guarded", Type: lvmdTypes.TypeThin, VolumeGroup: "vg", ThinPoolConfig: &lvmdTypes.ThinPoolConfig{Name: "pool", OverprovisionRatio: 1}, ReservationPolicy: &lvmdTypes.ReservationPolicy{BackupConcurrency: concurrency, OrdinaryVolumeCapBytes: 100, LockDirectory: t.TempDir()}}
	s := NewLVService(NewDeviceClassManager([]*lvmdTypes.DeviceClass{dc}), NewLvcreateOptionClassManager(nil), nil).(*lvService)
	s.reservationBackend = func(*lvmdTypes.DeviceClass) reservationBackend { return b }
	return s, b
}
func ordinary(t *testing.T, s *lvService, name string, size int64) *proto.LogicalVolume {
	t.Helper()
	r, e := s.CreateLV(context.Background(), &proto.CreateLVRequest{DeviceClass: "guarded", Name: name, SizeBytes: size})
	if e != nil {
		t.Fatal(e)
	}
	return r.Volume
}
func familyReq(source *proto.LogicalVolume, epoch uint64, id string) *proto.FamilyRequest {
	return &proto.FamilyRequest{DeviceClass: "guarded", SourceVolume: source.Name, PhysicalSourceUuid: source.PhysicalUuid, ExpectedClosedEpoch: epoch, RequestId: id}
}
func familyChild(t *testing.T, s *lvService, source *proto.LogicalVolume, token, name, access string, size int64) (*proto.LogicalVolume, error) {
	t.Helper()
	r, e := s.CreateLVSnapshot(context.Background(), &proto.CreateLVSnapshotRequest{DeviceClass: "guarded", SourceVolume: source.Name, ImmediateSourceUuid: source.PhysicalUuid, FamilyToken: token, Name: name, SizeBytes: size, AccessType: access})
	if e != nil {
		return nil, e
	}
	return r.Snapshot, nil
}
func budget(t *testing.T, s *lvService) *proto.GetPoolBudgetResponse {
	t.Helper()
	r, e := s.GetPoolBudget(context.Background(), &proto.GetPoolBudgetRequest{DeviceClass: "guarded"})
	if e != nil {
		t.Fatal(e)
	}
	return r
}

func TestReservationConcurrentClaimsAndOrphanCharge(t *testing.T) {
	s, b := reservationFixture(t, 140, 1)
	b.state.volumes["orphan"] = &proto.LogicalVolume{Name: "orphan", PhysicalUuid: "orphan-uuid", SizeBytes: 40, Pool: "pool"}
	var wg sync.WaitGroup
	codesFound := make(chan codes.Code, 2)
	for _, n := range []string{"one", "two"} {
		wg.Add(1)
		go func(n string) {
			defer wg.Done()
			_, e := s.CreateLV(context.Background(), &proto.CreateLVRequest{DeviceClass: "guarded", Name: n, SizeBytes: 20})
			codesFound <- status.Code(e)
		}(n)
	}
	wg.Wait()
	close(codesFound)
	ok, refused := 0, 0
	for c := range codesFound {
		if c == codes.OK {
			ok++
		}
		if c == codes.ResourceExhausted {
			refused++
		}
	}
	if ok != 1 || refused != 1 {
		t.Fatalf("competing claims: successful=%d refused=%d", ok, refused)
	}
	if got := budget(t, s); got.OrdinaryBytes != 60 || got.EffectiveBackupReserveBytes != 80 {
		t.Fatalf("orphan budget: %v", got)
	}
}

func TestReservationCancellationEpochAndPhysicalABA(t *testing.T) {
	s, b := reservationFixture(t, 500, 1)
	source := ordinary(t, s, "source", 20)
	req := familyReq(source, 0, "cancelled")
	closed, e := s.EndBackupFamily(context.Background(), req)
	if e != nil || closed.State != "closed" || closed.ClosedEpoch != 1 {
		t.Fatalf("early End: %v %v", closed, e)
	}
	if _, e = s.BeginBackupFamily(context.Background(), req); status.Code(e) != codes.FailedPrecondition {
		t.Fatalf("cancelled Begin: %v", e)
	}
	next := familyReq(source, 1, "next")
	active, e := s.BeginBackupFamily(context.Background(), next)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.EndBackupFamily(context.Background(), req); e != nil {
		t.Fatal(e)
	}
	current, e := s.GetBackupFamily(context.Background(), next)
	if e != nil || current.Token != active.Token || current.State != "active" {
		t.Fatalf("old End closed new family: %v %v", current, e)
	}
	b.state.volumes[source.Name].PhysicalUuid = "recreated"
	b.state.volumes[source.Name].Tags = nil
	if _, e = s.EndBackupFamily(context.Background(), next); status.Code(e) != codes.FailedPrecondition {
		t.Fatalf("physical ABA: %v", e)
	}
}

func TestReservationRetiringRestartAndConcurrencyReduction(t *testing.T) {
	s, b := reservationFixture(t, 500, 2)
	source := ordinary(t, s, "source", 20)
	req := familyReq(source, 0, "backup")
	f, e := s.BeginBackupFamily(context.Background(), req)
	if e != nil {
		t.Fatal(e)
	}
	if _, e := familyChild(t, s, source, f.Token, "snapshot", "ro", 20); e != nil {
		t.Fatal(e)
	}
	b.fail = "remove"
	if _, e = s.EndBackupFamily(context.Background(), req); e == nil {
		t.Fatal("failed remove closed family")
	}
	other := ordinary(t, s, "other", 20)
	dc, _ := s.dcmapper.DeviceClass("guarded")
	dc.ReservationPolicy.BackupConcurrency = 0
	if _, err := s.BeginBackupFamily(context.Background(), familyReq(other, 0, "new")); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("concurrency reduction admitted new family: %v", err)
	}
	// A fresh service holds no in-memory reservation state.
	restarted := NewLVService(s.dcmapper, s.ocmapper, nil).(*lvService)
	restarted.reservationBackend = s.reservationBackend
	if got := budget(t, restarted); got.OutstandingFamilyReservationBytes != 40 || got.EffectiveBackupReserveBytes != 40 {
		t.Fatalf("retiring budget disappeared: %v", got)
	}
	if _, e := familyChild(t, restarted, source, f.Token, "late", "ro", 20); status.Code(e) != codes.FailedPrecondition {
		t.Fatalf("retiring token allocated: %v", e)
	}
	closed, e := restarted.EndBackupFamily(context.Background(), req)
	if e != nil || closed.State != "closed" {
		t.Fatalf("closure retry: %v %v", closed, e)
	}
	if budget(t, restarted).OutstandingFamilyReservationBytes != 0 {
		t.Fatal("cleanup never released")
	}
}

func TestReservationImmutableChildrenAndSourceGrowth(t *testing.T) {
	s, _ := reservationFixture(t, 500, 1)
	source := ordinary(t, s, "source", 20)
	req := familyReq(source, 0, "backup")
	f, e := s.BeginBackupFamily(context.Background(), req)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.ResizeLV(context.Background(), &proto.ResizeLVRequest{DeviceClass: "guarded", Name: source.Name, SizeBytes: 30}); status.Code(e) != codes.ResourceExhausted {
		t.Fatalf("growth before capture: %v", e)
	}
	if _, err := familyChild(t, s, source, f.Token, "oversized", "ro", 21); status.Code(err) != codes.OutOfRange {
		t.Fatalf("oversized family child: %v", err)
	}
	snap, e := familyChild(t, s, source, f.Token, "snapshot", "ro", 20)
	if e != nil {
		t.Fatal(e)
	}
	if _, e := familyChild(t, s, source, f.Token, "second", "ro", 20); status.Code(e) != codes.FailedPrecondition {
		t.Fatalf("duplicate snapshot role: %v", e)
	}
	if _, e = s.ResizeLV(context.Background(), &proto.ResizeLVRequest{DeviceClass: "guarded", Name: source.Name, SizeBytes: 30}); e != nil {
		t.Fatal(e)
	}
	if _, e := familyChild(t, s, snap, f.Token, "clone", "rw", 20); e != nil {
		t.Fatal(e)
	}
	if _, e = s.EndBackupFamily(context.Background(), req); e != nil {
		t.Fatal(e)
	}
	if _, e = s.BeginBackupFamily(context.Background(), familyReq(source, 1, "new")); e != nil {
		t.Fatal(e)
	}
	if _, e := familyChild(t, s, snap, f.Token, "delayed-clone", "rw", 20); status.Code(e) != codes.FailedPrecondition {
		t.Fatalf("old token used new family: %v", e)
	}
}

func TestReservationPartialSnapshotAndResizeRemainCharged(t *testing.T) {
	for _, failure := range []string{"create", "resize", "activate"} {
		t.Run(failure, func(t *testing.T) {
			s, b := reservationFixture(t, 500, 1)
			source := ordinary(t, s, "source", 20)
			b.fail = failure
			_, e := s.CreateLVSnapshot(context.Background(), &proto.CreateLVSnapshotRequest{DeviceClass: "guarded", Name: "legacy-snapshot", SourceVolume: source.Name, SizeBytes: 30, AccessType: "rw"})
			if e == nil {
				t.Fatal("interruption succeeded")
			}
			if got := budget(t, s); got.OrdinaryBytes != 50 {
				t.Fatalf("partial target lost: %v", got)
			}
			r, e := s.CreateLVSnapshot(context.Background(), &proto.CreateLVSnapshotRequest{DeviceClass: "guarded", Name: "legacy-snapshot", SourceVolume: source.Name, SizeBytes: 30, AccessType: "rw"})
			if e != nil || r.Snapshot.SizeBytes != 30 {
				t.Fatalf("recovery: %v %v", r, e)
			}
		})
	}
}

func TestReservationOrphanFamilyAndUntrustedMetadata(t *testing.T) {
	s, b := reservationFixture(t, 500, 1)
	source := ordinary(t, s, "source", 20)
	req := familyReq(source, 0, "backup")
	f, e := s.BeginBackupFamily(context.Background(), req)
	if e != nil {
		t.Fatal(e)
	}
	if _, e := familyChild(t, s, source, f.Token, "snapshot", "ro", 20); e != nil {
		t.Fatal(e)
	}
	delete(b.state.volumes, source.Name)
	if got := budget(t, s); got.OutstandingFamilyReservationBytes != 40 || got.OrdinaryBytes != 0 {
		t.Fatalf("orphan family lost full reservation: %v", got)
	}
	if _, e := s.CreateLV(context.Background(), &proto.CreateLVRequest{DeviceClass: "guarded", Name: "forged", SizeBytes: 20, Tags: []string{familyPrefix + "forged"}}); status.Code(e) != codes.InvalidArgument {
		t.Fatalf("caller forged metadata: %v", e)
	}
	b.state.volumes["unknown"] = &proto.LogicalVolume{Name: "unknown", PhysicalUuid: "unknown-uuid", SizeBytes: 20, Tags: []string{familyPrefix + "malformed"}}
	if _, e := s.GetPoolBudget(context.Background(), &proto.GetPoolBudgetRequest{DeviceClass: "guarded"}); status.Code(e) != codes.Unavailable {
		t.Fatalf("malformed metadata became free: %v", e)
	}
}

func TestReservationAllocationQuantumAndPartialGrowth(t *testing.T) {
	s, b := reservationFixture(t, 500, 1)
	b.state.quantum = 4
	source := ordinary(t, s, "source", 21)
	if source.SizeBytes != 24 {
		t.Fatalf("unaligned physical target: %v", source)
	}
	b.fail = "resize"
	if _, e := s.ResizeLV(context.Background(), &proto.ResizeLVRequest{DeviceClass: "guarded", Name: source.Name, SizeBytes: 29}); status.Code(e) != codes.Unavailable {
		t.Fatalf("resize interruption: %v", e)
	}
	got := budget(t, s)
	if got.OrdinaryBytes != 32 || got.EffectiveBackupReserveBytes != 64 {
		t.Fatalf("partial growth intent lost: %v", got)
	}
	if b.state.volumes[source.Name].SizeBytes != 24 {
		t.Fatal("failed resize changed actual size")
	}
	if _, e := s.ResizeLV(context.Background(), &proto.ResizeLVRequest{DeviceClass: "guarded", Name: source.Name, SizeBytes: 29}); e != nil {
		t.Fatal(e)
	}
	if b.state.volumes[source.Name].SizeBytes != 32 {
		t.Fatal("resize did not recover retained target")
	}
}

func TestReservationTargetAcknowledgedBeforeActualGrowth(t *testing.T) {
	s, b := reservationFixture(t, 500, 1)
	source := ordinary(t, s, "source", 20)
	f, e := s.BeginBackupFamily(context.Background(), familyReq(source, 0, "backup"))
	if e != nil {
		t.Fatal(e)
	}
	request := &proto.ReserveLVTargetRequest{DeviceClass: "guarded", SourceVolume: source.Name, PhysicalSourceUuid: source.PhysicalUuid, TargetBytes: 30}
	ack, e := s.ReserveLVTarget(context.Background(), request)
	if e != nil || ack.ReservedBytes != 30 || ack.PhysicalUuid != source.PhysicalUuid {
		t.Fatalf("target acknowledgement: %v %v", ack, e)
	}
	if b.state.volumes[source.Name].SizeBytes != 20 {
		t.Fatal("reserving target resized the source")
	}
	if got := budget(t, s); got.OrdinaryBytes != 30 || got.EffectiveBackupReserveBytes != 60 {
		t.Fatalf("in-flight growth promise is not charged: %v", got)
	}
	request.TargetBytes = 25
	ack, e = s.ReserveLVTarget(context.Background(), request)
	if e != nil || ack.ReservedBytes != 30 {
		t.Fatalf("reservation decreased: %v %v", ack, e)
	}
	if _, e := familyChild(t, s, source, f.Token, "snapshot", "ro", 20); e != nil {
		t.Fatal(e)
	}
	if f.SourceSizeBytes != 20 {
		t.Fatal("target reservation changed frozen family size")
	}
	request.PhysicalSourceUuid = "recreated"
	if _, e := s.ReserveLVTarget(context.Background(), request); status.Code(e) != codes.FailedPrecondition {
		t.Fatalf("target accepted another physical incarnation: %v", e)
	}
}

func TestReservationIdempotencyDoesNotNotifyNewMetadata(t *testing.T) {
	s, _ := reservationFixture(t, 500, 1)
	source := ordinary(t, s, "source", 20)
	changes := 0
	s.notifyFunc = func() { changes++ }
	req := familyReq(source, 0, "backup")
	first, err := s.BeginBackupFamily(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	retry, err := s.BeginBackupFamily(context.Background(), req)
	if err != nil || retry.Token != first.Token {
		t.Fatalf("Begin retry: %v %v", retry, err)
	}
	if changes != 1 {
		t.Fatalf("idempotent Begin published new metadata: %d", changes)
	}
}

func TestReservationAcceptedTargetDoesNotBlockBackupBeforeGrowth(t *testing.T) {
	s, b := reservationFixture(t, 500, 1)
	source := ordinary(t, s, "source", 20)
	if _, err := s.ReserveLVTarget(context.Background(), &proto.ReserveLVTargetRequest{DeviceClass: "guarded", SourceVolume: source.Name, PhysicalSourceUuid: source.PhysicalUuid, TargetBytes: 30}); err != nil {
		t.Fatal(err)
	}
	family, err := s.BeginBackupFamily(context.Background(), familyReq(source, 0, "backup"))
	if err != nil {
		t.Fatalf("accepted ordinary target prevented backup before actual growth: %v", err)
	}
	if family.SourceSizeBytes != 20 || b.state.volumes[source.Name].SizeBytes != 20 {
		t.Fatal("Begin treated a future target as physical source size")
	}
	if got := budget(t, s); got.OrdinaryBytes != 30 || got.EffectiveBackupReserveBytes != 60 {
		t.Fatalf("Begin lost the accepted target charge: %v", got)
	}
	if _, err := s.ResizeLV(context.Background(), &proto.ResizeLVRequest{DeviceClass: "guarded", Name: source.Name, SizeBytes: 30}); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("actual growth escaped snapshot capture wait: %v", err)
	}
}
