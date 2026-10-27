package runners

import (
	"context"
	"encoding/json"
	"io"
	"testing"

	topolvmv1 "github.com/topolvm/topolvm/api/v1"
	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	"google.golang.org/grpc"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type budgetWatch struct {
	grpc.ClientStream
	frame *proto.WatchResponse
}

func (w *budgetWatch) Recv() (*proto.WatchResponse, error) {
	if w.frame == nil {
		return nil, io.EOF
	}
	f := w.frame
	w.frame = nil
	return f, nil
}
func TestReservationPublicationUsesOneFrameAndOnlyPhysicalMatches(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	node := &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: "node", UID: types.UID("node-incarnation")}}
	lv := &topolvmv1.LogicalVolume{ObjectMeta: metav1.ObjectMeta{Name: "represented-resource", UID: types.UID("opaque-volume-handle")}, Spec: topolvmv1.LogicalVolumeSpec{NodeName: "node", DeviceClass: "guarded"}, Status: topolvmv1.LogicalVolumeStatus{VolumeID: "opaque-volume-handle", PhysicalUUID: "physical-lv"}}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithObjects(node, lv).Build()
	exporter := &metricsExporter{client: kube, nodeName: "node"}
	watch := &budgetWatch{frame: &proto.WatchResponse{Items: []*proto.WatchItem{{DeviceClass: "guarded", ThinPool: &proto.ThinPoolItem{}, ReservationBudget: &proto.GetPoolBudgetResponse{ObservedAt: "2026-01-02T03:04:05Z", RefreshAfterSeconds: 37, PoolSizeBytes: 200, OrdinaryBytes: 95, EffectiveBackupReserveBytes: 60, LargestSizeBytes: 30, Allocations: []*proto.PhysicalAllocation{{Name: "opaque-volume-handle", PhysicalUuid: "physical-lv", ReservedBytes: 30, Ordinary: true}, {Name: "unclaimed", PhysicalUuid: "orphan", ReservedBytes: 65, Ordinary: true}, {Name: "temporary", PhysicalUuid: "temporary", ReservedBytes: 20, Ordinary: false}}}}}}}
	if err := exporter.updateNode(ctx, watch, make(chan NodeMetrics, 1)); err != nil {
		t.Fatal(err)
	}
	if err := kube.Get(ctx, client.ObjectKey{Name: "node"}, node); err != nil {
		t.Fatal(err)
	}
	var frame publishedReservationBudget
	if err := json.Unmarshal([]byte(node.Annotations[reservationAnnotationPrefix+"guarded"]), &frame); err != nil {
		t.Fatal(err)
	}
	if frame.OrdinaryBytes != 95 || frame.PoolSizeBytes != 200 || frame.EffectiveBackupReserveBytes != 60 || frame.ObservedAt != "2026-01-02T03:04:05Z" || frame.NodeUID != "node-incarnation" || frame.RefreshAfterSeconds != 37 {
		t.Fatalf("mixed or freshened frame: %+v", frame)
	}
	if len(frame.Allocations) != 1 || frame.Allocations[0].Name != "represented-resource" || frame.Allocations[0].PhysicalUUID != "physical-lv" || frame.Allocations[0].ReservedBytes != 30 {
		t.Fatalf("unmatched physical charge became a credit: %+v", frame.Allocations)
	}
}

func TestReservationPublicationPreservesOldStampOnIncompleteFrame(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	node := &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: "node", UID: types.UID("node-incarnation"), Annotations: map[string]string{reservationAnnotationPrefix + "guarded": "previous-frame"}}}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithObjects(node).Build()
	exporter := &metricsExporter{client: kube, nodeName: "node"}
	watch := &budgetWatch{frame: &proto.WatchResponse{Items: []*proto.WatchItem{{DeviceClass: "guarded", ThinPool: &proto.ThinPoolItem{}, ReservationBudget: &proto.GetPoolBudgetResponse{PoolSizeBytes: 200}}}}}
	if err := exporter.updateNode(ctx, watch, make(chan NodeMetrics, 1)); err == nil {
		t.Fatal("incomplete frame acquired a fresh stamp")
	}
	if err := kube.Get(ctx, client.ObjectKey{Name: "node"}, node); err != nil {
		t.Fatal(err)
	}
	if node.Annotations[reservationAnnotationPrefix+"guarded"] != "previous-frame" {
		t.Fatal("failed observation replaced the old frame")
	}
}
