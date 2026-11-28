// Test-only: an independent IEC 61850 MMS server (IEC61850bean, Apache-2.0) for the CI job "iec61850-interop".
// Never part of the product or its images.
//
//   java -cp iec61850bean.jar:asn1bean.jar:slf4j-api.jar:. Iec61850BeanServer MODEL.icd PORT
// Prints READY when listening. Commands on stdin, one per line:
//   float REF FC VALUE | bool REF FC true|false | int REF FC VALUE | dbpos REF FC 0..3 | quality REF FC BITS(13 chars of 0/1)
// Every write a client sends is printed as "WRITE <reference>", so tests can check that only RCB attributes were written.
import com.beanit.iec61850bean.*;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;

public class Iec61850BeanServer {
  public static void main(String[] args) throws Exception {
    List<ServerModel> models = SclParser.parse(args[0]);
    ServerModel model = models.get(0);
    ServerSap sap = new ServerSap(Integer.parseInt(args[1]), 0, java.net.InetAddress.getByName("127.0.0.1"), model, null);
    sap.startListening(new ServerEventListener() {
      public List<ServiceError> write(List<BasicDataAttribute> bdas) {
        for (BasicDataAttribute b : bdas) System.out.println("WRITE " + b.getReference() + " " + b.getFc());
        System.out.flush();
        return null;
      }
      public void serverStoppedListening(ServerSap s) {}
    });
    model = sap.getModelCopy();          // setValues takes attributes of a copy; the server holds the live model
    System.out.println("READY");
    System.out.flush();
    BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
    String line;
    while ((line = in.readLine()) != null) {
      String[] p = line.trim().split("\\s+");
      if (p.length < 4) { if (p[0].equals("quit")) break; continue; }
      BasicDataAttribute b = (BasicDataAttribute) model.findModelNode(p[1], Fc.fromString(p[2]));
      if (b == null) { System.out.println("NOTFOUND " + p[1]); System.out.flush(); continue; }
      switch (p[0]) {
        case "float": ((BdaFloat32) b).setFloat(Float.parseFloat(p[3])); break;
        case "bool": ((BdaBoolean) b).setValue(Boolean.parseBoolean(p[3])); break;
        case "int": ((BdaInt32) b).setValue(Integer.parseInt(p[3])); break;
        case "dbpos": ((BdaDoubleBitPos) b).setDoubleBitPos(BdaDoubleBitPos.DoubleBitPos.values()[Integer.parseInt(p[3])]); break;
        case "quality": {
          byte[] v = new byte[2];
          for (int i = 0; i < 13; i++) if (p[3].charAt(i) == '1') v[i / 8] |= (byte) (0x80 >> (i % 8));
          ((BdaQuality) b).setValue(v);
          break;
        }
        default: System.out.println("UNKNOWN " + p[0]); System.out.flush(); continue;
      }
      List<BasicDataAttribute> l = new ArrayList<>();
      l.add(b);
      sap.setValues(l);
      System.out.println("OK");
      System.out.flush();
    }
    sap.stop();
  }
}
