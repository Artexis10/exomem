package controller

import (
	"context"
	"testing"
	"time"

	"github.com/topolvm/topolvm"
	topolvmv1 "github.com/topolvm/topolvm/api/v1"
	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type familyProtocolClient struct {
	proto.LVServiceClient
	received *proto.FamilyRequest
}

func (c *familyProtocolClient) BeginBackupFamily(_ context.Context, req *proto.FamilyRequest, _ ...grpc.CallOption) (*proto.FamilyResponse, error) {
	c.received = req
	return &proto.FamilyResponse{State: "active", Token: "immutable-token", Epoch: 8, ClosedEpoch: 7, SourceSizeBytes: 20, RequestId: req.RequestId, PhysicalSourceUuid: req.PhysicalSourceUuid}, nil
}
func (c *familyProtocolClient) EndBackupFamily(_ context.Context, req *proto.FamilyRequest, _ ...grpc.CallOption) (*proto.FamilyResponse, error) {
	c.received = req
	return &proto.FamilyResponse{State: "closed", Epoch: 8, ClosedEpoch: 8, RequestId: req.RequestId, PhysicalSourceUuid: req.PhysicalSourceUuid}, nil
}
func TestReservationSourceRequestRetainsPreBeginIdentity(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	lv := &topolvmv1.LogicalVolume{ObjectMeta: metav1.ObjectMeta{Name: "source"}, Spec: topolvmv1.LogicalVolumeSpec{DeviceClass: "guarded", BackupFamilyRequest: &topolvmv1.BackupFamilyRequest{Operation: "Begin", PhysicalSourceUUID: "physical-source", ExpectedClosedEpoch: 7, RequestID: "backup-request"}}, Status: topolvmv1.LogicalVolumeStatus{VolumeID: "opaque-handle"}}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(lv).WithObjects(lv).Build()
	service := new(familyProtocolClient)
	r := NewLogicalVolumeReconcilerWithServices(kube, "node", nil, service)
	if err := r.reconcileBackupFamily(ctx, lv); err != nil {
		t.Fatal(err)
	}
	if service.received.SourceVolume != "opaque-handle" || service.received.ExpectedClosedEpoch != 7 || lv.Status.BackupFamily.Token != "immutable-token" {
		t.Fatalf("Begin transport: %v %v", service.received, lv.Status.BackupFamily)
	}
	if err := kube.Get(ctx, client.ObjectKeyFromObject(lv), lv); err != nil {
		t.Fatal(err)
	}
	lv.Spec.BackupFamilyRequest.Operation = "End"
	if err := r.reconcileBackupFamily(ctx, lv); err != nil {
		t.Fatal(err)
	}
	if service.received.ExpectedClosedEpoch != 7 || service.received.PhysicalSourceUuid != "physical-source" || service.received.RequestId != "backup-request" || lv.Status.BackupFamily.ClosedEpoch != 8 {
		t.Fatalf("End changed pre-Begin identity: %v %v", service.received, lv.Status.BackupFamily)
	}
}

type targetProtocolClient struct {
	proto.LVServiceClient
	received *proto.ReserveLVTargetRequest
	err      error
}

func (c *targetProtocolClient) ReserveLVTarget(_ context.Context, req *proto.ReserveLVTargetRequest, _ ...grpc.CallOption) (*proto.ReserveLVTargetResponse, error) {
	c.received = req
	if c.err != nil {
		return nil, c.err
	}
	return &proto.ReserveLVTargetResponse{PhysicalUuid: req.PhysicalSourceUuid, ReservedBytes: req.TargetBytes}, nil
}
func TestReservationTargetRefusalBindsCallerAndPreservesServingStatus(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	lv := &topolvmv1.LogicalVolume{ObjectMeta: metav1.ObjectMeta{Name: "source", Generation: 4}, Spec: topolvmv1.LogicalVolumeSpec{DeviceClass: "guarded", ReservationTargetBytes: 30, ReservationTargetPhysicalUUID: "submitted-physical-source"}, Status: topolvmv1.LogicalVolumeStatus{VolumeID: "opaque-handle", PhysicalUUID: "later-physical-status", Code: codes.OK}}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(lv).WithObjects(lv).Build()
	service := &targetProtocolClient{err: status.Error(codes.ResourceExhausted, "physical room is occupied")}
	r := NewLogicalVolumeReconcilerWithServices(kube, "node", nil, service)
	if err := r.reconcileReservationTarget(ctx, lv); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("no-room result did not retain controller retry: %v", err)
	}
	if service.received.PhysicalSourceUuid != "submitted-physical-source" {
		t.Fatal("retry adopted a later physical identity")
	}
	if lv.Status.Code != codes.OK || lv.Status.Message != "" {
		t.Fatal("growth refusal changed serving readiness")
	}
	result := lv.Status.ReservationTargetResult
	if result == nil || result.PhysicalUUID != "submitted-physical-source" || result.RequestedBytes != 30 || result.Code != int32(codes.ResourceExhausted) || result.ObservedGeneration != 4 || result.ReservedBytes != nil {
		t.Fatalf("refusal did not acknowledge this exact request: %+v", result)
	}
}

type reservationControllerClient struct {
	proto.LVServiceClient
	active bool
	room   bool
}

func (c *reservationControllerClient) GetBackupFamily(_ context.Context, req *proto.FamilyRequest, _ ...grpc.CallOption) (*proto.FamilyResponse, error) {
	return &proto.FamilyResponse{State: "closed", PhysicalSourceUuid: req.PhysicalSourceUuid}, nil
}
func (c *reservationControllerClient) EndBackupFamily(_ context.Context, req *proto.FamilyRequest, _ ...grpc.CallOption) (*proto.FamilyResponse, error) {
	c.active = false
	return &proto.FamilyResponse{State: "closed", Epoch: 1, ClosedEpoch: 1, RequestId: req.RequestId, PhysicalSourceUuid: req.PhysicalSourceUuid}, nil
}
func (c *reservationControllerClient) ReserveLVTarget(_ context.Context, req *proto.ReserveLVTargetRequest, _ ...grpc.CallOption) (*proto.ReserveLVTargetResponse, error) {
	if c.active || !c.room {
		return nil, status.Error(codes.ResourceExhausted, "physical room is occupied")
	}
	return &proto.ReserveLVTargetResponse{PhysicalUuid: req.PhysicalSourceUuid, ReservedBytes: req.TargetBytes}, nil
}
func (c *reservationControllerClient) RemoveLV(context.Context, *proto.RemoveLVRequest, ...grpc.CallOption) (*proto.Empty, error) {
	if c.active {
		return nil, status.Error(codes.ResourceExhausted, "source deletion waits for family closure")
	}
	return &proto.Empty{}, nil
}

func TestReservationDeletingSourceCanCloseItsFamily(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	deleted := metav1.NewTime(time.Now())
	lv := &topolvmv1.LogicalVolume{ObjectMeta: metav1.ObjectMeta{Name: "source", UID: types.UID("opaque-source"), DeletionTimestamp: &deleted, Finalizers: []string{topolvm.GetLogicalVolumeFinalizer()}}, Spec: topolvmv1.LogicalVolumeSpec{NodeName: "node", DeviceClass: "guarded", BackupFamilyRequest: &topolvmv1.BackupFamilyRequest{Operation: "End", PhysicalSourceUUID: "physical-source", RequestID: "backup"}}, Status: topolvmv1.LogicalVolumeStatus{VolumeID: "opaque-source", PhysicalUUID: "physical-source"}}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(lv).WithObjects(lv).Build()
	provider := &reservationControllerClient{active: true}
	r := NewLogicalVolumeReconcilerWithServices(kube, "node", nil, provider)
	if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKey{Name: "source"}}); err != nil {
		t.Fatalf("deletion cannot complete family closure: %v", err)
	}
	if err := kube.Get(ctx, client.ObjectKey{Name: "source"}, lv); err == nil && len(lv.Finalizers) != 0 {
		t.Fatal("family closure left source deletion blocked")
	} else if err != nil && !apierrors.IsNotFound(err) {
		t.Fatal(err)
	}
}

func TestReservationFamilyClosureUnblocksPendingTarget(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	size := resource.MustParse("20")
	lv := &topolvmv1.LogicalVolume{ObjectMeta: metav1.ObjectMeta{Name: "source", UID: types.UID("opaque-source"), Finalizers: []string{topolvm.GetLogicalVolumeFinalizer()}, Labels: map[string]string{topolvm.CreatedbyLabelKey: topolvm.CreatedbyLabelValue}}, Spec: topolvmv1.LogicalVolumeSpec{NodeName: "node", DeviceClass: "guarded", Size: size, ReservationTargetBytes: 30, ReservationTargetPhysicalUUID: "physical-source", BackupFamilyRequest: &topolvmv1.BackupFamilyRequest{Operation: "End", PhysicalSourceUUID: "physical-source", RequestID: "backup"}}, Status: topolvmv1.LogicalVolumeStatus{VolumeID: "opaque-source", PhysicalUUID: "physical-source", CurrentSize: &size}}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(lv).WithObjects(lv).Build()
	provider := &reservationControllerClient{active: true, room: true}
	r := NewLogicalVolumeReconcilerWithServices(kube, "node", nil, provider)
	if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKey{Name: "source"}}); err != nil {
		t.Fatalf("target refusal prevented the closure that releases room: %v", err)
	}
	if err := kube.Get(ctx, client.ObjectKey{Name: "source"}, lv); err != nil {
		t.Fatal(err)
	}
	if lv.Status.ReservedBytes != 30 || lv.Status.BackupFamily == nil || lv.Status.BackupFamily.State != "closed" {
		t.Fatalf("closure did not unblock target: %+v", lv.Status)
	}
}

func TestReservationNoRoomRetriesWithoutFailingServingVolume(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	size := resource.MustParse("20")
	lv := &topolvmv1.LogicalVolume{ObjectMeta: metav1.ObjectMeta{Name: "source", UID: types.UID("opaque-source"), Finalizers: []string{topolvm.GetLogicalVolumeFinalizer()}, Labels: map[string]string{topolvm.CreatedbyLabelKey: topolvm.CreatedbyLabelValue}}, Spec: topolvmv1.LogicalVolumeSpec{NodeName: "node", DeviceClass: "guarded", Size: size, ReservationTargetBytes: 30, ReservationTargetPhysicalUUID: "physical-source"}, Status: topolvmv1.LogicalVolumeStatus{VolumeID: "opaque-source", PhysicalUUID: "physical-source", CurrentSize: &size}}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(lv).WithObjects(lv).Build()
	provider := &reservationControllerClient{}
	r := NewLogicalVolumeReconcilerWithServices(kube, "node", nil, provider)
	request := ctrl.Request{NamespacedName: client.ObjectKey{Name: "source"}}
	if _, err := r.Reconcile(ctx, request); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("no room did not request controller backoff: %v", err)
	}
	if err := kube.Get(ctx, request.NamespacedName, lv); err != nil {
		t.Fatal(err)
	}
	if lv.Status.Code != codes.OK || lv.Status.ReservationTargetResult == nil || lv.Status.ReservationTargetResult.Code != int32(codes.ResourceExhausted) || lv.Spec.ReservationTargetBytes != 30 {
		t.Fatalf("refusal lost serving state or desired target: %+v", lv)
	}
	provider.room = true
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatalf("room became available but target stayed refused: %v", err)
	}
	if err := kube.Get(ctx, request.NamespacedName, lv); err != nil {
		t.Fatal(err)
	}
	if lv.Status.Code != codes.OK || lv.Status.ReservedBytes != 30 || lv.Status.ReservationTargetResult.Code != int32(codes.OK) {
		t.Fatalf("target retry did not recover: %+v", lv.Status)
	}
}
