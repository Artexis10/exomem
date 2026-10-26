package lvmd

import (
	"context"
	"fmt"
	"math"

	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	lvmdTypes "github.com/topolvm/topolvm/pkg/lvmd/types"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	gproto "google.golang.org/protobuf/proto"
)

func (s *lvService) guardedCreate(ctx context.Context, dc *lvmdTypes.DeviceClass, req *proto.CreateLVRequest) (*proto.CreateLVResponse, error) {
	if req.LvcreateOptionClass != "" {
		return nil, status.Error(codes.InvalidArgument, "guarded allocation does not permit arbitrary lvcreate options")
	}
	var volume *proto.LogicalVolume
	err := s.withReservation(ctx, dc, func(ctx context.Context, b reservationBackend, i *physicalInventory) error {
		target, err := alignedTarget(req.SizeBytes, i.quantum)
		if err != nil {
			return err
		}
		if target > dc.ReservationPolicy.OrdinaryVolumeCapBytes {
			return status.Error(codes.ResourceExhausted, "ordinary volume cap exceeded")
		}
		intent := &volumeIntent{Version: 1, Target: target}
		tag, err := encodeMetadata(intentPrefix, intent)
		if err != nil {
			return err
		}
		if existing := i.volumes[req.Name]; existing != nil {
			previous, _, err := intentOn(existing)
			if err != nil {
				return err
			}
			child, err := childOn(existing)
			if err != nil {
				return err
			}
			if child != nil || previous.SourceUUID != "" || previous.Target != target {
				return status.Error(codes.AlreadyExists, "physical volume has a different allocation intent")
			}
		} else {
			i.volumes[req.Name] = &proto.LogicalVolume{Name: req.Name, PhysicalUuid: "prospective", SizeBytes: int64(target), Tags: append(append([]string{}, req.Tags...), tag)}
			if err := roomFor(i, dc.ReservationPolicy); err != nil {
				return err
			}
			if err := b.create(ctx, req.Name, target, i.volumes[req.Name].Tags); err != nil {
				return err
			}
		}
		fresh, err := b.inventory(ctx)
		if err != nil {
			return err
		}
		volume = fresh.volumes[req.Name]
		if volume == nil || uint64(volume.SizeBytes) < target {
			return status.Error(codes.ResourceExhausted, "created volume awaits physical observation")
		}
		volume.ReservedTargetBytes = target
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &proto.CreateLVResponse{Volume: volume}, nil
}

func familyForChild(i *physicalInventory, req *proto.CreateLVSnapshotRequest, target uint64) (*proto.LogicalVolume, *familyRecord, *childRecord, error) {
	var identity familyIdentity
	if err := decodeMetadata(req.FamilyToken, &identity); err != nil {
		return nil, nil, nil, status.Error(codes.InvalidArgument, "invalid family token")
	}
	var root *proto.LogicalVolume
	for _, lv := range i.volumes {
		if lv.PhysicalUuid == identity.PhysicalUUID {
			root = lv
			break
		}
	}
	if root == nil {
		return nil, nil, nil, status.Error(codes.FailedPrecondition, "family physical source is absent")
	}
	f, _, err := familyOn(root)
	if err != nil {
		return nil, nil, nil, err
	}
	if f.State != familyActive || f.familyIdentity != identity {
		return nil, nil, nil, status.Error(codes.FailedPrecondition, "family token is revoked")
	}
	if target > f.SourceSize {
		return nil, nil, nil, status.Error(codes.OutOfRange, "child exceeds frozen family size")
	}
	source := i.volumes[req.SourceVolume]
	if source == nil {
		return nil, nil, nil, status.Error(codes.FailedPrecondition, "immediate physical source is absent")
	}
	if req.ImmediateSourceUuid == "" || req.ImmediateSourceUuid != source.PhysicalUuid {
		return nil, nil, nil, status.Error(codes.FailedPrecondition, "immediate source physical incarnation changed")
	}
	role := familySnapshotRole
	switch req.AccessType {
	case "ro":
		if source.PhysicalUuid != root.PhysicalUuid {
			return nil, nil, nil, status.Error(codes.FailedPrecondition, "snapshot source does not match family root")
		}
		if f.Snapshot != "" && f.Snapshot != req.Name {
			return nil, nil, nil, status.Error(codes.FailedPrecondition, "snapshot role already has a fixed child")
		}
		f.Snapshot = req.Name
	case "rw":
		role = familyCloneRole
		ownership, err := childOn(source)
		if err != nil {
			return nil, nil, nil, err
		}
		if ownership == nil || ownership.Token != req.FamilyToken || ownership.Role != familySnapshotRole || f.Snapshot != source.Name || !f.Captured {
			return nil, nil, nil, status.Error(codes.FailedPrecondition, "clone source is not this family's captured snapshot")
		}
		if f.Clone != "" && f.Clone != req.Name {
			return nil, nil, nil, status.Error(codes.FailedPrecondition, "clone role already has a fixed child")
		}
		f.Clone = req.Name
	default:
		return nil, nil, nil, status.Error(codes.InvalidArgument, "invalid snapshot access type")
	}
	return root, f, &childRecord{Version: 1, Token: req.FamilyToken, SourceUUID: source.PhysicalUuid, Role: role, SourceSize: f.SourceSize, Target: target}, nil
}
func (s *lvService) guardedSnapshot(ctx context.Context, dc *lvmdTypes.DeviceClass, req *proto.CreateLVSnapshotRequest) (*proto.CreateLVSnapshotResponse, error) {
	var volume *proto.LogicalVolume
	err := s.withReservation(ctx, dc, func(ctx context.Context, b reservationBackend, i *physicalInventory) error {
		if req.AccessType != "ro" && req.AccessType != "rw" {
			return status.Error(codes.InvalidArgument, "invalid snapshot access type")
		}
		source := i.volumes[req.SourceVolume]
		if source == nil {
			if req.FamilyToken != "" {
				return status.Error(codes.FailedPrecondition, "family immediate source is absent")
			}
			return status.Error(codes.NotFound, "snapshot source is absent")
		}
		if req.ImmediateSourceUuid != "" && req.ImmediateSourceUuid != source.PhysicalUuid {
			return status.Error(codes.FailedPrecondition, "immediate source incarnation changed")
		}
		size := req.SizeBytes
		if size == 0 {
			size = source.SizeBytes
		}
		target, err := alignedTarget(size, i.quantum)
		if err != nil {
			return err
		}
		var root *proto.LogicalVolume
		var family *familyRecord
		var child *childRecord
		var tag string
		if req.FamilyToken != "" {
			root, family, child, err = familyForChild(i, req, target)
			if err != nil {
				return err
			}
			tag, err = encodeMetadata(childPrefix, child)
			if err != nil {
				return err
			}
		} else {
			if target > dc.ReservationPolicy.OrdinaryVolumeCapBytes {
				return status.Error(codes.ResourceExhausted, "ordinary snapshot cap exceeded")
			}
			tag, err = encodeMetadata(intentPrefix, &volumeIntent{Version: 1, Target: target, SourceUUID: source.PhysicalUuid, Access: req.AccessType})
			if err != nil {
				return err
			}
		}
		if existing := i.volumes[req.Name]; existing != nil {
			if err := validateSnapshotRetry(existing, source, child, target, req.AccessType); err != nil {
				return err
			}
		} else {
			if uint64(source.SizeBytes) > target {
				return status.Error(codes.OutOfRange, "requested snapshot is smaller than its immediate source")
			}
			if child == nil {
				i.volumes[req.Name] = &proto.LogicalVolume{Name: req.Name, PhysicalUuid: "prospective", SizeBytes: int64(target), Tags: []string{tag}}
				if err := roomFor(i, dc.ReservationPolicy); err != nil {
					return err
				}
			} else {
				// Commit each role's name before its tagged lvcreate command.
				if err := writeFamily(ctx, b, root, family); err != nil {
					return err
				}
			}
			if err := b.snapshot(ctx, source.Name, req.Name, append(append([]string{}, req.Tags...), tag)); err != nil {
				return err
			}
		}
		// Repeating every remaining command recovers creation, resize and activation cuts.
		if err := b.resize(ctx, req.Name, target); err != nil {
			return err
		}
		if err := b.activate(ctx, req.Name, req.AccessType); err != nil {
			return err
		}
		fresh, err := b.inventory(ctx)
		if err != nil {
			return err
		}
		volume = fresh.volumes[req.Name]
		if volume == nil || volume.SizeBytes != int64(target) {
			return status.Error(codes.ResourceExhausted, "snapshot awaits complete physical observation")
		}
		if err := markSnapshotCaptured(ctx, b, root, family, child, fresh); err != nil {
			return err
		}

		volume.ReservedTargetBytes = target
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &proto.CreateLVSnapshotResponse{Snapshot: volume}, nil
}
func (s *lvService) guardedResize(ctx context.Context, dc *lvmdTypes.DeviceClass, req *proto.ResizeLVRequest) (*proto.ResizeLVResponse, error) {
	var size int64
	err := s.withReservation(ctx, dc, func(ctx context.Context, b reservationBackend, i *physicalInventory) error {
		lv := i.volumes[req.Name]
		if lv == nil {
			return status.Error(codes.NotFound, "physical volume is absent")
		}
		target, err := alignedTarget(req.SizeBytes, i.quantum)
		if err != nil {
			return err
		}
		size = lv.SizeBytes
		if target <= uint64(lv.SizeBytes) {
			return nil
		}
		child, err := childOn(lv)
		if err != nil {
			return err
		}
		if child != nil {
			return status.Error(codes.FailedPrecondition, "temporary child size is immutable")
		}
		if target > dc.ReservationPolicy.OrdinaryVolumeCapBytes {
			return status.Error(codes.ResourceExhausted, "ordinary volume cap exceeded")
		}
		f, _, err := familyOn(lv)
		if err != nil {
			return err
		}
		if f.State != familyClosed && !f.Captured {
			return status.Error(codes.ResourceExhausted, "source growth waits for snapshot capture or family closure")
		}
		intent, old, err := intentOn(lv)
		if err != nil {
			return err
		}
		intent.Version = 1
		intent.Target = max(intent.Target, target)
		tag, err := encodeMetadata(intentPrefix, intent)
		if err != nil {
			return err
		}
		copyLV := gproto.Clone(lv).(*proto.LogicalVolume)
		copyLV.Tags = append([]string{}, lv.Tags...)
		for n, t := range copyLV.Tags {
			if t == old {
				copyLV.Tags = append(copyLV.Tags[:n], copyLV.Tags[n+1:]...)
				break
			}
		}
		copyLV.Tags = append(copyLV.Tags, tag)
		i.volumes[req.Name] = copyLV
		if err := roomFor(i, dc.ReservationPolicy); err != nil {
			return err
		}
		if err := b.replaceTag(ctx, lv.Name, old, tag); err != nil {
			return err
		}
		if err := b.resize(ctx, lv.Name, intent.Target); err != nil {
			return err
		}
		fresh, err := b.inventory(ctx)
		if err != nil {
			return err
		}
		observed := fresh.volumes[lv.Name]
		if observed == nil || uint64(observed.SizeBytes) < intent.Target {
			return status.Error(codes.ResourceExhausted, "resize awaits physical target")
		}
		size = observed.SizeBytes
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &proto.ResizeLVResponse{SizeBytes: size}, nil
}
func (s *lvService) guardedRemove(ctx context.Context, dc *lvmdTypes.DeviceClass, req *proto.RemoveLVRequest) (*proto.Empty, error) {
	err := s.withReservation(ctx, dc, func(ctx context.Context, b reservationBackend, i *physicalInventory) error {
		lv := i.volumes[req.Name]
		if lv == nil {
			return status.Error(codes.NotFound, "physical volume is absent")
		}
		f, _, err := familyOn(lv)
		if err != nil {
			return err
		}
		if f.State != familyClosed {
			return status.Error(codes.ResourceExhausted, "source deletion waits for family closure")
		}
		if err := b.remove(ctx, lv.Name); err != nil {
			return err
		}
		fresh, err := b.inventory(ctx)
		if err != nil {
			return err
		}
		if fresh.volumes[lv.Name] != nil {
			return status.Error(codes.ResourceExhausted, "deletion awaits physical absence")
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &proto.Empty{}, nil
}

func reservedTarget(lv *proto.LogicalVolume) (uint64, error) {
	intent, _, err := intentOn(lv)
	if err != nil {
		return 0, fmt.Errorf("volume intent: %w", err)
	}
	child, err := childOn(lv)
	if err != nil {
		return 0, err
	}
	target := intent.Target
	if child != nil {
		target = max(target, child.Target)
	}
	return target, nil
}

func (s *lvService) ReserveLVTarget(ctx context.Context, req *proto.ReserveLVTargetRequest) (*proto.ReserveLVTargetResponse, error) {
	dc, err := s.reservationClass(req.DeviceClass)
	if err != nil {
		return nil, err
	}
	var response *proto.ReserveLVTargetResponse
	err = s.withReservation(ctx, dc, func(ctx context.Context, b reservationBackend, i *physicalInventory) error {
		lv := i.volumes[req.SourceVolume]
		if lv == nil {
			return status.Error(codes.NotFound, "physical reservation source is absent")
		}
		if req.PhysicalSourceUuid == "" || req.PhysicalSourceUuid != lv.PhysicalUuid {
			return status.Error(codes.FailedPrecondition, "physical reservation source incarnation changed")
		}
		if req.TargetBytes > math.MaxInt64 {
			return status.Error(codes.OutOfRange, "reservation target exceeds CSI range")
		}
		target, err := alignedTarget(int64(req.TargetBytes), i.quantum)
		if err != nil {
			return err
		}
		child, err := childOn(lv)
		if err != nil {
			return err
		}
		if child != nil {
			return status.Error(codes.FailedPrecondition, "temporary child reservation is immutable")
		}
		intent, old, err := intentOn(lv)
		if err != nil {
			return err
		}
		accepted := max(intent.Target, uint64(lv.SizeBytes))
		if target <= accepted {
			response = &proto.ReserveLVTargetResponse{ReservedBytes: accepted, PhysicalUuid: lv.PhysicalUuid}
			return nil
		}
		intent.Version = 1
		intent.Target = target
		if intent.Target > dc.ReservationPolicy.OrdinaryVolumeCapBytes {
			return status.Error(codes.ResourceExhausted, "ordinary volume cap exceeded")
		}
		tag, err := encodeMetadata(intentPrefix, intent)
		if err != nil {
			return err
		}
		copyLV := gproto.Clone(lv).(*proto.LogicalVolume)
		copyLV.Tags = append([]string{}, lv.Tags...)
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
		if err := b.replaceTag(ctx, lv.Name, old, tag); err != nil {
			return err
		}
		response = &proto.ReserveLVTargetResponse{ReservedBytes: intent.Target, PhysicalUuid: lv.PhysicalUuid}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return response, nil
}

func validateSnapshotRetry(existing, source *proto.LogicalVolume, child *childRecord, target uint64, access string) error {
	existingChild, err := childOn(existing)
	if err != nil {
		return err
	}
	if child != nil {
		if existingChild == nil || *existingChild != *child {
			return status.Error(codes.AlreadyExists, "snapshot has a different immutable family binding")
		}
		return nil
	}
	intent, _, err := intentOn(existing)
	if err != nil {
		return err
	}
	if existingChild != nil || intent.SourceUUID != source.PhysicalUuid || intent.Target != target || intent.Access != access {
		return status.Error(codes.AlreadyExists, "snapshot has a different allocation intent")
	}
	return nil
}

func markSnapshotCaptured(ctx context.Context, b reservationBackend, root *proto.LogicalVolume, family *familyRecord, child *childRecord, fresh *physicalInventory) error {
	if family != nil && child.Role == familySnapshotRole && !family.Captured {
		current := fresh.volumes[root.Name]
		if current == nil || current.PhysicalUuid != root.PhysicalUuid {
			return status.Error(codes.Unavailable, "family source observation changed")
		}
		family.Captured = true
		if err := writeFamily(ctx, b, current, family); err != nil {
			return err
		}
	}
	return nil
}
