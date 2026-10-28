package controller

import (
	"context"
	"math"
	"reflect"

	topolvmv1 "github.com/topolvm/topolvm/api/v1"
	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (r *LogicalVolumeReconciler) reconcileBackupFamily(ctx context.Context, lv *topolvmv1.LogicalVolume) error {
	request := lv.Spec.BackupFamilyRequest
	if request == nil {
		response, err := r.lvService.GetBackupFamily(ctx, &proto.FamilyRequest{DeviceClass: lv.Spec.DeviceClass, SourceVolume: lv.Status.VolumeID, PhysicalSourceUuid: lv.Status.PhysicalUUID})
		if status.Code(err) == codes.Unimplemented {
			return nil
		}
		if err != nil {
			return err
		}
		return r.publishBackupFamily(ctx, lv, response)
	}
	if request.ExpectedClosedEpoch < 0 {
		return status.Error(codes.InvalidArgument, "family epoch must not be negative")
	}
	req := &proto.FamilyRequest{DeviceClass: lv.Spec.DeviceClass, SourceVolume: lv.Status.VolumeID, PhysicalSourceUuid: request.PhysicalSourceUUID, ExpectedClosedEpoch: uint64(request.ExpectedClosedEpoch), RequestId: request.RequestID}
	var response *proto.FamilyResponse
	var err error
	// Begin and End are fixed operations in the reservation protocol.
	switch request.Operation {
	case topolvmv1.BackupFamilyBegin:
		response, err = r.lvService.BeginBackupFamily(ctx, req)
	case topolvmv1.BackupFamilyEnd:
		response, err = r.lvService.EndBackupFamily(ctx, req)
	default:
		return status.Error(codes.InvalidArgument, "unknown family operation")
	}
	if err != nil {
		return err
	}
	return r.publishBackupFamily(ctx, lv, response)
}

func (r *LogicalVolumeReconciler) publishBackupFamily(ctx context.Context, lv *topolvmv1.LogicalVolume, response *proto.FamilyResponse) error {
	if response.Epoch > math.MaxInt64 || response.ClosedEpoch > math.MaxInt64 || response.SourceSizeBytes > math.MaxInt64 {
		return status.Error(codes.OutOfRange, "provider family exceeds Kubernetes integer range")
	}
	next := &topolvmv1.BackupFamilyStatus{Token: response.Token, SourceSizeBytes: int64(response.SourceSizeBytes), State: response.State, ClosedEpoch: int64(response.ClosedEpoch), Epoch: int64(response.Epoch), RequestID: response.RequestId, PhysicalSourceUUID: response.PhysicalSourceUuid}
	if lv.Status.BackupFamily != nil && *lv.Status.BackupFamily == *next {
		return nil
	}
	lv.Status.BackupFamily = next
	return r.client.Status().Update(ctx, lv)
}

func (r *LogicalVolumeReconciler) reconcileReservationTarget(ctx context.Context, lv *topolvmv1.LogicalVolume) error {
	if lv.Spec.ReservationTargetBytes == 0 {
		return nil
	}
	if lv.Spec.ReservationTargetBytes < 0 || lv.Spec.ReservationTargetPhysicalUUID == "" {
		return status.Error(codes.InvalidArgument, "reservation target requires its submitted physical identity and positive bytes")
	}
	response, err := r.lvService.ReserveLVTarget(ctx, &proto.ReserveLVTargetRequest{DeviceClass: lv.Spec.DeviceClass, SourceVolume: lv.Status.VolumeID, PhysicalSourceUuid: lv.Spec.ReservationTargetPhysicalUUID, TargetBytes: uint64(lv.Spec.ReservationTargetBytes)})
	result := &topolvmv1.ReservationTargetResult{PhysicalUUID: lv.Spec.ReservationTargetPhysicalUUID, RequestedBytes: lv.Spec.ReservationTargetBytes, ObservedGeneration: lv.Generation}
	if err != nil {
		result.Code = int32(status.Code(err))
		result.Message = status.Convert(err).Message()
	} else if response.PhysicalUuid != lv.Spec.ReservationTargetPhysicalUUID || response.ReservedBytes < uint64(lv.Spec.ReservationTargetBytes) || response.ReservedBytes > math.MaxInt64 {
		err = status.Error(codes.Unavailable, "provider reservation acknowledgement is pending")
		result.Code = int32(codes.Unavailable)
		result.Message = status.Convert(err).Message()
	} else {
		accepted := int64(response.ReservedBytes)
		result.ReservedBytes = &accepted
		lv.Status.ReservedBytes = accepted
	}
	if !reflect.DeepEqual(lv.Status.ReservationTargetResult, result) {
		lv.Status.ReservationTargetResult = result
		if updateErr := r.client.Status().Update(ctx, lv); updateErr != nil {
			return updateErr
		}
	}
	// The existing controller backoff retries refusal without changing serving status.
	return err
}
