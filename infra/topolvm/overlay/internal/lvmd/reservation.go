package lvmd

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/topolvm/topolvm/internal/lvmd/command"
	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	lvmdTypes "github.com/topolvm/topolvm/pkg/lvmd/types"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	gproto "google.golang.org/protobuf/proto"
)

// These prefixes, states and roles are the versioned, closed on-disk allocation protocol.
const (
	familyPrefix       = "topolvm.reservation.v1."
	childPrefix        = "topolvm.reservation.child.v1."
	intentPrefix       = "topolvm.intent.v1."
	familyActive       = "active"
	familyRetiring     = "retiring"
	familyClosed       = "closed"
	familySnapshotRole = "snapshot"
	familyCloneRole    = "clone"
)

type familyIdentity struct {
	PhysicalUUID string `json:"u"`
	Epoch        uint64 `json:"e"`
	RequestID    string `json:"r"`
}
type familyRecord struct {
	Version int `json:"v"`
	familyIdentity
	State      string `json:"s"`
	SourceSize uint64 `json:"z"`
	Snapshot   string `json:"p,omitempty"`
	Clone      string `json:"c,omitempty"`
	Captured   bool   `json:"a,omitempty"`
}
type childRecord struct {
	Version    int    `json:"v"`
	Token      string `json:"t"`
	SourceUUID string `json:"u"`
	Role       string `json:"r"`
	SourceSize uint64 `json:"z"`
	Target     uint64 `json:"n"`
}
type volumeIntent struct {
	Version    int    `json:"v"`
	Target     uint64 `json:"n"`
	SourceUUID string `json:"u,omitempty"`
	Access     string `json:"a,omitempty"`
}

func encodeMetadata(prefix string, record any) (string, error) {
	data, err := json.Marshal(record)
	if err != nil {
		return "", err
	}
	tag := prefix + base64.RawURLEncoding.EncodeToString(data)
	// LVM tags have a fixed 1024-byte limit; metadata is bounded to one tag.
	if len(tag) > 1024 {
		return "", status.Error(codes.InvalidArgument, "reservation metadata exceeds LVM tag size")
	}
	return tag, nil
}
func decodeMetadata(encoded string, record any) error {
	if len(encoded) > 1024 {
		return fmt.Errorf("oversized reservation metadata")
	}
	data, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(record); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return fmt.Errorf("trailing reservation metadata")
	}
	return nil
}
func metadataTag(tags []string, prefix string, record any) (string, error) {
	found := ""
	for _, tag := range tags {
		if strings.HasPrefix(tag, prefix) {
			if found != "" {
				return "", fmt.Errorf("duplicate reservation metadata")
			}
			found = tag
		}
	}
	if found != "" {
		if err := decodeMetadata(strings.TrimPrefix(found, prefix), record); err != nil {
			return "", err
		}
	}
	return found, nil
}
func familyOn(lv *proto.LogicalVolume) (*familyRecord, string, error) {
	for _, tag := range lv.Tags {
		if strings.HasPrefix(tag, "topolvm.reservation.") && !strings.HasPrefix(tag, familyPrefix) && !strings.HasPrefix(tag, childPrefix) {
			return nil, "", fmt.Errorf("unknown reservation metadata version")
		}
		if strings.HasPrefix(tag, "topolvm.intent.") && !strings.HasPrefix(tag, intentPrefix) {
			return nil, "", fmt.Errorf("unknown intent metadata version")
		}
	}
	f := &familyRecord{Version: 1, familyIdentity: familyIdentity{PhysicalUUID: lv.PhysicalUuid}, State: familyClosed}
	tag, err := metadataTag(lv.Tags, familyPrefix, f)
	if err != nil {
		return nil, "", err
	}
	if f.Version != 1 || f.PhysicalUUID != lv.PhysicalUuid || f.PhysicalUUID == "" {
		return nil, "", fmt.Errorf("family physical identity mismatch")
	}
	switch f.State {
	case familyClosed:
	case familyActive, familyRetiring:
		if f.SourceSize == 0 || f.Epoch == 0 || f.RequestID == "" {
			return nil, "", fmt.Errorf("invalid outstanding family")
		}
	default:
		return nil, "", fmt.Errorf("invalid family state")
	}
	return f, tag, nil
}
func childOn(lv *proto.LogicalVolume) (*childRecord, error) {
	c := new(childRecord)
	tag, err := metadataTag(lv.Tags, childPrefix, c)
	if err != nil || tag == "" {
		return nil, err
	}
	// Snapshot and clone are the two fixed roles in the provider family protocol.
	if c.Version != 1 || c.SourceSize == 0 || c.Target == 0 || c.Target > c.SourceSize || c.SourceUUID == "" || (c.Role != familySnapshotRole && c.Role != familyCloneRole) {
		return nil, fmt.Errorf("invalid family child")
	}
	var id familyIdentity
	if err := decodeMetadata(c.Token, &id); err != nil || id.PhysicalUUID == "" || id.Epoch == 0 || id.RequestID == "" {
		return nil, fmt.Errorf("invalid child token")
	}
	return c, nil
}
func intentOn(lv *proto.LogicalVolume) (*volumeIntent, string, error) {
	i := new(volumeIntent)
	tag, err := metadataTag(lv.Tags, intentPrefix, i)
	if err != nil {
		return nil, "", err
	}
	if tag != "" && (i.Version != 1 || i.Target == 0) {
		return nil, "", fmt.Errorf("invalid volume intent")
	}
	return i, tag, nil
}
func tokenFor(f *familyRecord) string { token, _ := encodeMetadata("", f.familyIdentity); return token }
func responseFor(f *familyRecord) *proto.FamilyResponse {
	r := &proto.FamilyResponse{State: f.State, Epoch: f.Epoch, RequestId: f.RequestID, PhysicalSourceUuid: f.PhysicalUUID, SourceSizeBytes: f.SourceSize}
	if f.State == familyClosed {
		r.ClosedEpoch = f.Epoch
	} else {
		r.ClosedEpoch = f.Epoch - 1
		r.Token = tokenFor(f)
	}
	return r
}
func validCallerTags(tags []string) error {
	for _, tag := range tags {
		if strings.HasPrefix(tag, "topolvm.reservation.") || strings.HasPrefix(tag, "topolvm.intent.") {
			return status.Error(codes.InvalidArgument, "caller cannot set reserved allocation metadata")
		}
	}
	return nil
}
func addBytes(a, b uint64) (uint64, error) {
	if math.MaxUint64-a < b {
		return 0, status.Error(codes.ResourceExhausted, "reservation size overflow")
	}
	return a + b, nil
}
func doubleBytes(a uint64) (uint64, error) { return addBytes(a, a) }
func alignedTarget(size int64, quantum uint64) (uint64, error) {
	if size <= 0 || quantum == 0 {
		return 0, status.Error(codes.InvalidArgument, "positive size and observed allocation quantum are required")
	}
	target := uint64(size)
	remainder := target % quantum
	if remainder != 0 {
		var err error
		target, err = addBytes(target, quantum-remainder)
		if err != nil {
			return 0, err
		}
	}
	if target > math.MaxInt64 {
		return 0, status.Error(codes.OutOfRange, "aligned size exceeds CSI range")
	}
	return target, nil
}
func computeBudget(i *physicalInventory, p *lvmdTypes.ReservationPolicy) (*proto.GetPoolBudgetResponse, int, error) {
	r := &proto.GetPoolBudgetResponse{PoolSizeBytes: i.total, BackupConcurrency: p.BackupConcurrency, RefreshAfterSeconds: uint64(WatchRefreshInterval / time.Second)}
	families := map[string]uint64{}
	roots := map[string]*familyRecord{}
	for _, lv := range i.volumes {

		f, tag, err := familyOn(lv)
		if err != nil {
			return nil, 0, err
		}
		if tag != "" && f.State != familyClosed {
			token := tokenFor(f)
			families[token] = f.SourceSize
			roots[token] = f
		}
	}
	for _, lv := range i.volumes {
		if lv.SizeBytes <= 0 || lv.PhysicalUuid == "" {
			return nil, 0, fmt.Errorf("incomplete physical volume inventory")
		}
		intent, _, err := intentOn(lv)
		if err != nil {
			return nil, 0, err
		}
		charge := max(uint64(lv.SizeBytes), intent.Target)
		c, err := childOn(lv)
		if err != nil {
			return nil, 0, err
		}
		ordinary := c == nil
		if c != nil {
			charge = max(charge, c.Target)
			if charge > c.SourceSize {
				return nil, 0, fmt.Errorf("family child exceeds frozen reservation")
			}
			if previous, ok := families[c.Token]; ok && previous != c.SourceSize {
				return nil, 0, fmt.Errorf("conflicting family size")
			}
			if root := roots[c.Token]; root != nil {
				if (c.Role == familySnapshotRole && root.Snapshot != lv.Name) || (c.Role == familyCloneRole && root.Clone != lv.Name) {
					return nil, 0, fmt.Errorf("family child does not match fixed role identity")
				}
			}
			// A tagged child whose source vanished retains its entire family charge.
			families[c.Token] = c.SourceSize
		}
		if ordinary {
			r.OrdinaryBytes, err = addBytes(r.OrdinaryBytes, charge)
			if err != nil {
				return nil, 0, err
			}
			r.LargestSizeBytes = max(r.LargestSizeBytes, charge)
		}
		r.Allocations = append(r.Allocations, &proto.PhysicalAllocation{Name: lv.Name, PhysicalUuid: lv.PhysicalUuid, Pool: lv.Pool, SizeBytes: uint64(lv.SizeBytes), ReservedBytes: charge, Ordinary: ordinary, Tags: lv.Tags})
	}
	for _, size := range families {
		charge, err := doubleBytes(size)
		if err != nil {
			return nil, 0, err
		}
		r.OutstandingFamilyReservationBytes, err = addBytes(r.OutstandingFamilyReservationBytes, charge)
		if err != nil {
			return nil, 0, err
		}
		r.LargestSizeBytes = max(r.LargestSizeBytes, size)
	}
	base, err := doubleBytes(r.LargestSizeBytes)
	if err != nil {
		return nil, 0, err
	}
	if p.BackupConcurrency != 0 && base > math.MaxUint64/p.BackupConcurrency {
		return nil, 0, status.Error(codes.ResourceExhausted, "concurrency reservation overflow")
	}
	r.EffectiveBackupReserveBytes = max(base*p.BackupConcurrency, r.OutstandingFamilyReservationBytes)
	sort.Slice(r.Allocations, func(a, b int) bool { return r.Allocations[a].Name < r.Allocations[b].Name })
	return r, len(families), nil
}
func roomFor(i *physicalInventory, p *lvmdTypes.ReservationPolicy) error {
	budget, _, err := computeBudget(i, p)
	if err != nil {
		return err
	}
	total, err := addBytes(budget.OrdinaryBytes, budget.EffectiveBackupReserveBytes)
	if err != nil {
		return err
	}
	if total > budget.PoolSizeBytes {
		return status.Error(codes.ResourceExhausted, "physical pool cannot preserve backup reserve")
	}
	return nil
}
func (s *lvService) withReservation(ctx context.Context, dc *lvmdTypes.DeviceClass, run func(context.Context, reservationBackend, *physicalInventory) error) error {
	p := dc.ReservationPolicy
	if p == nil {
		return status.Error(codes.FailedPrecondition, "reservation policy is not enabled")
	}
	err := command.WithPoolTransaction(ctx, p.LockDirectory, dc.VolumeGroup+"/"+dc.ThinPoolConfig.Name, func(ctx context.Context) error {
		var b reservationBackend = &lvmReservationBackend{dc: dc}
		if s.reservationBackend != nil {
			b = s.reservationBackend(dc)
		}
		b = notifyingReservationBackend{reservationBackend: b, notify: s.notify}
		i, err := b.inventory(ctx)
		if err != nil {
			return err
		}
		if _, _, err := computeBudget(i, p); err != nil {
			return err
		}
		if err := run(ctx, b, i); err != nil {
			return err
		}
		final, err := b.inventory(ctx)
		if err != nil {
			return err
		}
		_, _, err = computeBudget(final, p)
		return err
	})
	if err == nil {
		return nil
	}
	if _, ok := status.FromError(err); ok {
		return err
	}
	if ctx.Err() != nil {
		return status.FromContextError(ctx.Err()).Err()
	}
	// Observation and command failures retry; neither makes unknown bytes free.
	return status.Errorf(codes.Unavailable, "provider transaction pending: %v", err)
}
func (s *lvService) reservationClass(name string) (*lvmdTypes.DeviceClass, error) {
	dc, err := s.dcmapper.DeviceClass(name)
	if err != nil {
		return nil, status.Error(codes.NotFound, err.Error())
	}
	return dc, nil
}
func writeFamily(ctx context.Context, b reservationBackend, lv *proto.LogicalVolume, f *familyRecord) error {
	_, old, err := familyOn(lv)
	if err != nil {
		return err
	}
	tag, err := encodeMetadata(familyPrefix, f)
	if err != nil {
		return err
	}
	if err = b.replaceTag(ctx, lv.Name, old, tag); err != nil {
		return err
	}
	if old != "" {
		for n, t := range lv.Tags {
			if t == old {
				lv.Tags = append(lv.Tags[:n], lv.Tags[n+1:]...)
				break
			}
		}
	}
	lv.Tags = append(lv.Tags, tag)
	return nil
}
func sourceFamily(i *physicalInventory, req *proto.FamilyRequest) (*proto.LogicalVolume, *familyRecord, error) {
	lv := i.volumes[req.SourceVolume]
	if lv == nil {
		return nil, nil, status.Error(codes.NotFound, "physical family source is unavailable")
	}
	if req.PhysicalSourceUuid == "" || lv.PhysicalUuid != req.PhysicalSourceUuid {
		return nil, nil, status.Error(codes.FailedPrecondition, "physical source incarnation changed")
	}
	f, _, err := familyOn(lv)
	return lv, f, err
}
func (s *lvService) GetPoolBudget(ctx context.Context, req *proto.GetPoolBudgetRequest) (*proto.GetPoolBudgetResponse, error) {
	dc, err := s.reservationClass(req.DeviceClass)
	if err != nil {
		return nil, err
	}
	var response *proto.GetPoolBudgetResponse
	err = s.withReservation(ctx, dc, func(_ context.Context, _ reservationBackend, i *physicalInventory) error {
		var e error
		response, _, e = computeBudget(i, dc.ReservationPolicy)
		if e == nil {
			response.ObservedAt = time.Now().UTC().Format(time.RFC3339Nano)
		}
		return e
	})
	return response, err
}
func (s *lvService) GetBackupFamily(ctx context.Context, req *proto.FamilyRequest) (*proto.FamilyResponse, error) {
	dc, err := s.reservationClass(req.DeviceClass)
	if err != nil {
		return nil, err
	}
	if dc.ReservationPolicy == nil {
		return nil, status.Error(codes.Unimplemented, "reservation policy is not enabled")
	}
	var response *proto.FamilyResponse
	err = s.withReservation(ctx, dc, func(_ context.Context, _ reservationBackend, i *physicalInventory) error {
		_, f, e := sourceFamily(i, req)
		if e != nil {
			return e
		}
		response = responseFor(f)
		return nil
	})
	return response, err
}
func (s *lvService) BeginBackupFamily(ctx context.Context, req *proto.FamilyRequest) (*proto.FamilyResponse, error) {
	dc, err := s.reservationClass(req.DeviceClass)
	if err != nil {
		return nil, err
	}
	var response *proto.FamilyResponse
	err = s.withReservation(ctx, dc, func(ctx context.Context, b reservationBackend, i *physicalInventory) error {
		lv, f, err := sourceFamily(i, req)
		if err != nil {
			return err
		}
		if req.RequestId == "" || len(req.RequestId) > 128 {
			return status.Error(codes.InvalidArgument, "family request identity must contain 1 to 128 bytes")
		}
		if f.State == familyActive && f.RequestID == req.RequestId && f.Epoch > 0 && f.Epoch-1 == req.ExpectedClosedEpoch {
			response = responseFor(f)
			return nil
		}
		if f.State != familyClosed || f.Epoch != req.ExpectedClosedEpoch || f.RequestID == req.RequestId {
			return status.Error(codes.FailedPrecondition, "family epoch or request is closed or occupied")
		}
		if c, err := childOn(lv); err != nil {
			return err
		} else if c != nil {
			return status.Error(codes.FailedPrecondition, "family source is itself a temporary child")
		}
		_, count, err := computeBudget(i, dc.ReservationPolicy)
		if err != nil {
			return err
		}
		if uint64(count) >= dc.ReservationPolicy.BackupConcurrency {
			return status.Error(codes.ResourceExhausted, "backup family concurrency is occupied")
		}
		if f.Epoch == math.MaxUint64 {
			return status.Error(codes.OutOfRange, "family epoch exhausted")
		}
		f = &familyRecord{Version: 1, familyIdentity: familyIdentity{PhysicalUUID: lv.PhysicalUuid, Epoch: f.Epoch + 1, RequestID: req.RequestId}, State: familyActive, SourceSize: uint64(lv.SizeBytes)}
		copyLV := gproto.Clone(lv).(*proto.LogicalVolume)
		copyLV.Tags = append([]string{}, lv.Tags...)
		tag, err := encodeMetadata(familyPrefix, f)
		if err != nil {
			return err
		}
		_, old, _ := familyOn(lv)
		for n, t := range copyLV.Tags {
			if t == old {
				copyLV.Tags = append(copyLV.Tags[:n], copyLV.Tags[n+1:]...)
				break
			}
		}
		copyLV.Tags = append(copyLV.Tags, tag)
		i.volumes[lv.Name] = copyLV
		if err := roomFor(i, dc.ReservationPolicy); err != nil {
			return err
		}
		if err := writeFamily(ctx, b, lv, f); err != nil {
			return err
		}
		response = responseFor(f)
		return nil
	})
	return response, err
}
func (s *lvService) EndBackupFamily(ctx context.Context, req *proto.FamilyRequest) (*proto.FamilyResponse, error) {
	dc, err := s.reservationClass(req.DeviceClass)
	if err != nil {
		return nil, err
	}
	var response *proto.FamilyResponse
	err = s.withReservation(ctx, dc, func(ctx context.Context, b reservationBackend, i *physicalInventory) error {
		lv, f, err := sourceFamily(i, req)
		if err != nil {
			return err
		}
		if req.RequestId == "" || len(req.RequestId) > 128 {
			return status.Error(codes.InvalidArgument, "family request identity must contain 1 to 128 bytes")
		}
		if f.State == familyClosed && f.Epoch == req.ExpectedClosedEpoch {
			if f.Epoch == math.MaxUint64 {
				return status.Error(codes.OutOfRange, "family epoch exhausted")
			}
			f = &familyRecord{Version: 1, familyIdentity: familyIdentity{PhysicalUUID: lv.PhysicalUuid, Epoch: f.Epoch + 1, RequestID: req.RequestId}, State: familyClosed}
			if err := writeFamily(ctx, b, lv, f); err != nil {
				return err
			}
			response = responseFor(f)
			return nil
		}
		if f.RequestID != req.RequestId || f.Epoch == 0 || f.Epoch-1 != req.ExpectedClosedEpoch {
			if req.ExpectedClosedEpoch < f.Epoch {
				response = responseFor(f)
				return nil
			}
			return status.Error(codes.FailedPrecondition, "End does not match current family epoch")
		}
		if f.State == familyClosed {
			response = responseFor(f)
			return nil
		}
		f.State = familyRetiring
		if err := writeFamily(ctx, b, lv, f); err != nil {
			return err
		}
		// Revocation commits before deletion. Only immutable owned children qualify.
		token := tokenFor(f)
		for _, name := range []string{f.Clone, f.Snapshot} {
			if name == "" {
				continue
			}
			child := i.volumes[name]
			if child == nil {
				continue
			}
			ownership, err := childOn(child)
			if err != nil {
				return err
			}
			if ownership == nil || ownership.Token != token {
				return status.Error(codes.FailedPrecondition, "family child name belongs to another physical allocation")
			}
			if err := b.remove(ctx, name); err != nil {
				return err
			}
		}
		fresh, err := b.inventory(ctx)
		if err != nil {
			return err
		}
		for _, child := range fresh.volumes {
			ownership, err := childOn(child)
			if err != nil {
				return err
			}
			if ownership != nil && ownership.Token == token {
				return status.Error(codes.ResourceExhausted, "family cleanup awaits physical absence")
			}
		}
		current := fresh.volumes[lv.Name]
		if current == nil || current.PhysicalUuid != lv.PhysicalUuid {
			return status.Error(codes.Unavailable, "family source changed during cleanup")
		}
		f.State = familyClosed
		if err := writeFamily(ctx, b, current, f); err != nil {
			return err
		}
		response = responseFor(f)
		return nil
	})
	return response, err
}
